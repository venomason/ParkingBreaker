"""Synthetic HTTP fixtures exercise the real SDK, without credentials or network."""

import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from parkingbreaker import case_state as cs
from parkingbreaker.payment_coordinator import run_payment_step
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.payment_records import (
    PaymentClient,
    PaymentCursor,
    PaymentOutcome,
    outcome_json,
)
from parkingbreaker.prices import CERTIFIED_CENTS
from parkingbreaker.stripe_payment import StripePaymentClient
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import CaseConflict, SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_transaction_lifecycle import NOW, letter

REF = "pi_Original123"
CHARGE = "ch_Original123"
EXPIRY = int((NOW + timedelta(days=5)).timestamp())
VERSION = "offline-version-fixture"  # Arbitrary explicit config; not a claimed usable API version.


def fixture(status: str = "requires_capture", expiry: object = EXPIRY) -> dict[str, object]:
    received = 2999 if status == "succeeded" else 0
    charge: dict[str, object] = {
        "created": int(NOW.timestamp()),
        "id": CHARGE,
        "object": "charge",
        "payment_intent": REF,
        "amount": 2999,
        "currency": "usd",
        "amount_captured": received,
        "captured": status == "succeeded",
        "paid": True,
        "status": "succeeded",
        "payment_method_details": {"type": "card", "card": {"capture_before": expiry}},
        "billing_details": {"name": "PRIVATE PERSON"},
    }
    return {
        "id": REF,
        "object": "payment_intent",
        "capture_method": "manual",
        "amount": 2999,
        "currency": "usd",
        "amount_received": received,
        "amount_capturable": 2999 if status == "requires_capture" else 0,
        "status": status,
        "latest_charge": charge,
        "client_secret": "PRIVATE RESPONSE SECRET",
        "metadata": {"private": "PRIVATE PERSON"},
    }


class Transport:
    def __init__(self, *responses: object, status: int = 200) -> None:
        self.responses = list(responses)
        self.status = status
        self.calls: list[tuple[str, str, dict[str, list[str]], str | None, str | None]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        post_data: object = None,
    ) -> tuple[str, int, Mapping[str, str]]:
        assert headers is not None
        parsed = urlsplit(url)
        assert post_data is None or isinstance(post_data, str)
        params = parse_qs(parsed.query if post_data is None else post_data)
        self.calls.append(
            (
                method,
                parsed.path,
                params,
                headers.get("Idempotency-Key"),
                headers.get("Stripe-Version"),
            )
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        body = response if isinstance(response, str) else json.dumps(response)
        return body, self.status, {"stripe-should-retry": "true"}


def client(transport: Transport) -> StripePaymentClient:
    return StripePaymentClient(
        secret_key="offline-placeholder",
        api_version=VERSION,
        transport=transport,
    )


def charge(obj: dict[str, object]) -> dict[str, object]:
    value = obj["latest_charge"]
    assert isinstance(value, dict)
    return value


def test_sdk_exact_request_boundary() -> None:
    transport = Transport(fixture(), fixture("succeeded"), fixture("canceled"))
    adapter: PaymentClient = client(transport)
    held = adapter.retrieve(REF)
    assert held.status == "requires_capture"
    assert held.authorization_inspection is not None
    assert held.authorization_inspection.expires_at == datetime.fromtimestamp(EXPIRY, UTC)
    assert adapter.capture(REF, 2999, "capture-key", final_capture=True).status == "succeeded"
    assert adapter.release(REF, "cancel-key").status == "canceled"
    assert transport.calls == [
        ("get", f"/v1/payment_intents/{REF}", {"expand[0]": ["latest_charge"]}, None, VERSION),
        (
            "post",
            f"/v1/payment_intents/{REF}/capture",
            {
                "amount_to_capture": ["2999"],
                "expand[0]": ["latest_charge"],
            },
            "capture-key",
            VERSION,
        ),
        (
            "post",
            f"/v1/payment_intents/{REF}/cancel",
            {
                "expand[0]": ["latest_charge"],
            },
            "cancel-key",
            VERSION,
        ),
    ]


@pytest.mark.parametrize("seconds", [2 * 86400, 4 * 86400 + 18 * 3600, 30 * 86400])
def test_actual_expiry_and_typed_inspection(seconds: int) -> None:
    expiry = int(NOW.timestamp()) + seconds
    inspection = client(Transport(fixture(expiry=expiry))).inspect_authorization(REF)
    assert inspection is not None
    assert inspection.original_charge_ref == CHARGE
    assert inspection.expires_at == NOW + timedelta(seconds=seconds)


@pytest.mark.parametrize("expiry", [None, "1234", True, 0, -1, 253402300800, 12.5])
def test_missing_or_invalid_expiry_never_invents_clock(expiry: object) -> None:
    result = client(Transport(fixture(expiry=expiry))).retrieve(REF)
    assert result.status == "uncertain"
    assert result.capture_before is None
    assert result.authorization_inspection is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "pi_Replacement"),
        ("object", "charge"),
        ("currency", "eur"),
        ("amount", 2800),
        ("amount", True),
        ("amount", "2999"),
        ("amount_received", 100),
        ("amount_capturable", 2800),
        ("capture_method", "automatic_delayed"),
        ("capture_method", "automatic"),
        ("latest_charge", "ch_Unexpanded"),
        ("latest_charge", None),
        ("status", "processing"),
        ("payment_method_options", {"card": {"capture_method": "automatic_delayed"}}),
        ("payment_method_options", {"card": {"request_multicapture": "if_available"}}),
    ],
)
def test_reject_invalid_pi(field: str, value: object) -> None:
    obj = fixture()
    obj[field] = value
    assert client(Transport(obj)).retrieve(REF).status == "uncertain"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "bad-charge"),
        ("payment_intent", "pi_Replacement"),
        ("object", "payment_intent"),
        ("currency", "eur"),
        ("amount", 2800),
        ("amount_captured", 100),
        ("captured", True),
        ("paid", False),
        ("status", "failed"),
        (
            "payment_method_details",
            {
                "type": "card",
                "card": {
                    "capture_before": EXPIRY,
                    "multicapture": {"status": "available"},
                },
            },
        ),
    ],
)
def test_reject_invalid_charge(field: str, value: object) -> None:
    obj = fixture()
    charge(obj)[field] = value
    assert client(Transport(obj)).retrieve(REF).status == "uncertain"


@pytest.mark.parametrize("status", ["succeeded", "canceled"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount_received", 2800),
        ("amount_capturable", 1),
        ("latest_charge", "ch_Unexpanded"),
    ],
)
def test_contradictory_terminal_money(status: str, field: str, value: object) -> None:
    obj = fixture(status)
    obj[field] = value
    assert client(Transport(obj)).retrieve(REF).status == "uncertain"


def test_canceled_without_charge_is_external_confirmation() -> None:
    obj = fixture("canceled")
    obj["latest_charge"] = None
    assert client(Transport(obj)).retrieve(REF).status == "canceled"


@pytest.mark.parametrize(
    ("status", "classification"),
    [
        (400, "ambiguous_error"),
        (402, "ambiguous_error"),
        (401, "authentication"),
        (403, "authentication"),
        (404, "not_found"),
        (409, "conflict"),
        (429, "rate_limit"),
        (500, "server"),
        (503, "server"),
    ],
)
@pytest.mark.parametrize("method", ["retrieve", "capture", "release"])
def test_errors_uncertain_without_retries(status: int, classification: str, method: str) -> None:
    # Even a purported authoritative success embedded in a non-2xx cannot prove money.
    transport = Transport(
        {
            "error": {
                "message": "PRIVATE TEXT",
                "payment_intent": fixture(
                    "succeeded",
                ),
            }
        },
        status=status,
    )
    adapter = client(transport)
    result = (
        adapter.retrieve(REF)
        if method == "retrieve"
        else adapter.capture(REF, 2999, "key", final_capture=True)
        if method == "capture"
        else adapter.release(REF, "key")
    )
    assert result.status == "uncertain"
    assert result.classification == classification
    assert len(transport.calls) == 1
    assert "PRIVATE" not in outcome_json(result)


@pytest.mark.parametrize("error", [TimeoutError("PRIVATE TEXT"), ConnectionError("PRIVATE TEXT")])
def test_transport_failures_uncertain(error: Exception) -> None:
    transport = Transport(error)
    result = client(transport).capture(REF, 2999, "key", final_capture=True)
    assert result.status == "uncertain"
    assert result.classification == "transport"
    assert len(transport.calls) == 1
    assert "PRIVATE" not in outcome_json(result)


@pytest.mark.parametrize("body", ["{", "null", "[]", "42"])
def test_malformed_json(body: str) -> None:
    assert client(Transport(body)).retrieve(REF).status == "uncertain"


@pytest.mark.parametrize("mode", [True, False])
def test_sdk_preserves_provider_environment_on_original_charge(mode: bool) -> None:
    body = fixture("succeeded")
    body["livemode"] = mode
    charge = body["latest_charge"]
    assert isinstance(charge, dict)
    charge["livemode"] = mode
    result = client(Transport(body)).retrieve(REF)
    assert result.status == "succeeded"
    assert result.livemode is mode

    charge["livemode"] = not mode
    assert client(Transport(body)).retrieve(REF).status == "uncertain"


def test_invalid_provider_environment_stays_out_of_sdk_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="stripe")
    body = fixture("succeeded")
    body["livemode"] = "PRIVATE INVALID VALUE"
    assert client(Transport(body)).retrieve(REF).status == "uncertain"
    assert "PRIVATE" not in caplog.text


def test_safe_sdk_logging_and_repr(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="stripe")
    adapter = client(Transport(fixture()))
    result = adapter.retrieve(REF)
    assert result.status == "requires_capture"
    assert "PRIVATE" not in caplog.text
    assert "offline-placeholder" not in caplog.text
    assert "offline-placeholder" not in repr(adapter)
    assert "PRIVATE" not in repr(result)
    transport = Transport({"error": {"message": "PRIVATE ERROR"}}, status=400)
    client(transport).retrieve(REF)
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize(("amount", "final"), [(2800, True), (2999, False), (True, True)])
def test_invalid_capture_has_no_request(amount: int, final: bool) -> None:
    transport = Transport()
    with pytest.raises(ValueError, match="full final"):
        client(transport).capture(REF, amount, "key", final_capture=final)
    assert transport.calls == []


@pytest.mark.parametrize("ref", ["auth", "pi_", "pi_a/b", "pi_a?x", "ch_123"])
def test_noncanonical_reference_has_no_request(ref: str) -> None:
    transport = Transport()
    with pytest.raises(ValueError, match="reference"):
        client(transport).retrieve(ref)
    assert transport.calls == []


def test_explicit_configuration_required() -> None:
    with pytest.raises(ValueError, match="explicit"):
        StripePaymentClient(secret_key="", api_version=VERSION)
    with pytest.raises(ValueError, match="explicit"):
        StripePaymentClient(secret_key="offline-placeholder", api_version="")


def test_only_authoritative_bound_failed_charge_supports_decline() -> None:
    obj = fixture("requires_payment_method")
    charge(obj).update({"status": "failed", "paid": False})
    obj["last_payment_error"] = {"type": "card_error", "code": "card_declined", "charge": CHARGE}
    assert client(Transport(obj)).retrieve(REF).status == "declined"
    obj["last_payment_error"] = {
        "type": "card_error",
        "code": "card_declined",
        "charge": "ch_Other",
    }
    assert client(Transport(obj)).retrieve(REF).status == "uncertain"


def make_case(store: SqliteTransactionStore, expiry: datetime) -> CaseRecord:
    store.save_workspace("workspace", Workspace(), NOW)
    case = store.start_case("workspace", REF, NOW, expiry)
    for event in (
        cs.ResearchComplete(),
        cs.DraftReady(),
        cs.CustomerApproved(),
        cs.SubmitRequested(),
        cs.SubmitConfirmed("lob-fixture"),
    ):
        case = letter(store, case, event)
    return case


def step(
    store: SqliteTransactionStore,
    adapter: PaymentClient,
    case: CaseRecord,
    cursor: PaymentCursor | None = None,
    at: datetime = NOW,
    compensation: bool = False,
) -> PaymentCursor:
    current = store.get_case(case.case_token)
    assert current is not None
    return run_payment_step(
        store,
        adapter,
        case.case_token,
        expected_case_revision=current.revision,
        expected_cursor_revision=None if cursor is None else cursor.revision,
        operation_id=f"operation-{0 if cursor is None else cursor.revision}",
        key="namespace",
        clock=lambda: at,
        compensation_effect_ids=("refund", "compensation-log") if compensation else (),
    )


def test_inspection_starts_case_and_capture_restart_preserves_anchors(tmp_path: Path) -> None:
    transport = Transport(fixture(), fixture(), fixture("succeeded"))
    adapter = client(transport)
    inspection = adapter.inspect_authorization(REF)
    assert inspection is not None
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = make_case(store, inspection.expires_at)
        cursor = step(store, adapter, case)
        assert cursor.contract_version == 3
        assert cursor.outcome.authorization_inspection == inspection
    with SqliteTransactionStore(path) as store:
        assert store.payment_cursor(case.case_token) == cursor
        saved = store.get_case(case.case_token)
        assert saved is not None
        assert (saved.authorized_at, saved.authorization_expires_at) == (NOW, inspection.expires_at)
        cursor = step(store, adapter, saved, cursor, NOW + timedelta(days=2))
        assert cursor.done
        assert cursor.first_at == NOW
        assert cursor.final_capture is True
        assert transport.calls[-1][3] == cursor.mutation_key
        assert store.payment_cursor(case.case_token) == cursor
        with pytest.raises(CaseConflict):
            store.start_case("workspace", REF, NOW + timedelta(days=2), inspection.expires_at)
    assert [call[0] for call in transport.calls] == ["get", "get", "post"]


@pytest.mark.parametrize("days", [2, 7])
def test_expiry_mismatch_refuses_capture_and_preserves_anchor(tmp_path: Path, days: int) -> None:
    transport = Transport(fixture(), fixture())
    adapter = client(transport)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        expiry = NOW + timedelta(days=days)
        case = make_case(store, expiry)
        cursor = step(store, adapter, case)
        assert cursor.outcome.classification == "expiry_mismatch"
        cursor = step(store, adapter, case, cursor, cursor.next_at)
        assert not cursor.done
        assert all(call[0] == "get" for call in transport.calls)
        assert store.get_case(case.case_token) == case
        assert case.authorization_expires_at == expiry


def test_old_fictional_expiry_cannot_authorize_late_capture(tmp_path: Path) -> None:
    transport = Transport(fixture(expiry=int((NOW + timedelta(days=2)).timestamp())))
    adapter = client(transport)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = make_case(store, NOW + timedelta(days=7))
        fake = FakePaymentClient(REF)
        old = step(store, fake, case)  # Legacy v2 full object, no actual expiry.
        cursor = step(store, adapter, case, old, NOW + timedelta(days=3))
        assert cursor.contract_version == 3
        assert cursor.call_kind == "retrieve"
        assert cursor.outcome.classification == "expiry_mismatch"
        assert len(transport.calls) == 1
        assert transport.calls[0][0] == "get"


def test_already_captured_retrieval_has_no_second_mutation(tmp_path: Path) -> None:
    transport = Transport(fixture("succeeded"))
    adapter = client(transport)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = make_case(store, datetime.fromtimestamp(EXPIRY, UTC))
        cursor = step(store, adapter, case)
        assert cursor.done
        assert step(store, adapter, case, cursor) == cursor
        assert len(transport.calls) == 1


def test_cached_500_reconciles_before_any_new_mutation(tmp_path: Path) -> None:
    transport = Transport(fixture(), {"error": {"message": "PRIVATE"}}, fixture("succeeded"))
    adapter = client(transport)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = make_case(store, datetime.fromtimestamp(EXPIRY, UTC))
        cursor = step(store, adapter, case)
        transport.status = 500
        cursor = step(store, adapter, case, cursor)
        assert cursor.outcome.classification == "server"
        assert cursor.failed_attempts == 0
        assert not cursor.done
        transport.status = 200
        cursor = step(store, adapter, case, cursor, cursor.next_at)
        assert cursor.done
        assert [call[0] for call in transport.calls] == ["get", "post", "get"]


def test_old_outcome_encoding_unchanged_and_evidence_explicit() -> None:
    old = PaymentOutcome("uncertain", "auth")
    assert (
        outcome_json(old)
        == '{"amount_cents":2999,"authorization_ref":"auth","charge_ref":null,"status":"uncertain"}'
    )
    with pytest.raises(ValueError, match="unversioned"):
        outcome_json(replace(old, capture_before=EXPIRY))


@pytest.mark.parametrize("status", ["canceled", "succeeded"])
def test_release_confirmation_and_captured_release_compensation(
    tmp_path: Path, status: str
) -> None:
    transport = Transport(fixture(), fixture(status))
    adapter = client(transport)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        store.save_workspace("workspace", Workspace(), NOW)
        case = store.start_case("workspace", REF, NOW, datetime.fromtimestamp(EXPIRY, UTC))
        case = letter(store, case, cs.WindowExpired())
        cursor = step(store, adapter, case)
        cursor = step(store, adapter, case, cursor, compensation=status == "succeeded")
        assert cursor.done is (status == "canceled")
        assert cursor.outcome.status == status
        assert transport.calls[-1][1] == f"/v1/payment_intents/{REF}/cancel"
        saved = store.get_case(case.case_token)
        assert saved is not None
        if status == "canceled":
            assert saved == case
        else:
            from parkingbreaker.case_status import CaseStatus
            from parkingbreaker.payment_state import PaymentEffect, PaymentState

            assert saved.payment_state is PaymentState.CAPTURED
            assert saved.case_status is CaseStatus.IN_PROGRESS
            assert [
                e.effect for e in store.pending_effects(case.case_token) if e.machine == "payment"
            ] == [PaymentEffect.REFUND_FULL, PaymentEffect.LOG_COMPENSATION]
        assert store.payment_cursor(case.case_token) == cursor


def test_missing_capture_before_and_expired_timestamp_do_not_prove_expiry() -> None:
    assert client(Transport(fixture(expiry=None))).retrieve(REF).status == "uncertain"
    obj = fixture(expiry=int((NOW + timedelta(hours=1)).timestamp()))
    assert client(Transport(obj)).retrieve(REF).status == "requires_capture"
    # No clock inside adapter: only a canceled original object confirms release/lapse.
    obj["status"] = "canceled"
    obj["amount_capturable"] = 0
    assert client(Transport(obj)).retrieve(REF).status == "canceled"


def test_timeout_then_changed_original_charge_remains_unresolved(tmp_path: Path) -> None:
    replacement = fixture("succeeded")
    charge(replacement)["id"] = "ch_Replacement123"
    transport = Transport(fixture(), TimeoutError("PRIVATE"), replacement)
    adapter = client(transport)
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = make_case(store, datetime.fromtimestamp(EXPIRY, UTC))
        cursor = step(store, adapter, case)
        cursor = step(store, adapter, case, cursor)
        assert cursor.authorization_charge_ref == CHARGE
    with SqliteTransactionStore(path) as store:
        assert store.payment_cursor(case.case_token) == cursor
        cursor = step(store, adapter, case, cursor, cursor.next_at)
        assert cursor.outcome.classification == "charge_mismatch"
        assert cursor.authorization_charge_ref == CHARGE
        assert not cursor.done
        assert store.get_case(case.case_token) == case
        assert store.payment_cursor(case.case_token) == cursor


@pytest.mark.parametrize("key", ["", " ", "x" * 256])
def test_invalid_key_has_no_call(key: str) -> None:
    transport = Transport()
    with pytest.raises(ValueError, match="key"):
        client(transport).release(REF, key)
    assert transport.calls == []


@pytest.mark.parametrize("field", ["status", "paid", "captured", "amount_captured"])
def test_canceled_contradictory_charge_refused(field: str) -> None:
    obj = fixture("canceled")
    charge(obj)[field] = {
        "status": "pending",
        "paid": False,
        "captured": True,
        "amount_captured": 2999,
    }[field]
    assert client(Transport(obj)).retrieve(REF).status == "uncertain"


def test_mutation_without_confirmed_terminal_object_stays_uncertain() -> None:
    result = client(Transport(fixture())).capture(REF, 2999, "key", final_capture=True)
    assert result.status == "uncertain"
    assert result.classification == "unconfirmed_mutation"


@pytest.mark.parametrize("created", [None, True, EXPIRY, EXPIRY + 1])
def test_expiry_requires_valid_charge_creation_evidence(created: object) -> None:
    obj = fixture()
    charge(obj)["created"] = created
    assert client(Transport(obj)).retrieve(REF).status == "uncertain"


def test_duplicate_json_identity_is_malformed() -> None:
    body = json.dumps(fixture()).replace(
        '"id": "pi_Original123"', '"id": "pi_Wrong", "id": "pi_Original123"', 1
    )
    result = client(Transport(body)).retrieve(REF)
    assert result.status == "uncertain"
    assert result.classification == "invalid_object"


def test_mismatch_retains_original_charge_to_refuse_reauthorization(tmp_path: Path) -> None:
    changed = fixture(expiry=int((NOW + timedelta(days=7)).timestamp()))
    charge(changed)["id"] = "ch_Replacement123"
    transport = Transport(fixture(), changed)
    adapter = client(transport)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = make_case(store, NOW + timedelta(days=7))
        cursor = step(store, adapter, case)
        assert cursor.authorization_charge_ref == CHARGE
        cursor = step(store, adapter, case, cursor, cursor.next_at)
        assert cursor.outcome.classification == "charge_mismatch"
        assert not cursor.done
        assert all(call[0] == "get" for call in transport.calls)
        assert store.payment_cursor(case.case_token) == cursor


@pytest.mark.parametrize("status", ["succeeded", "canceled"])
def test_terminal_object_needs_no_inferred_expiry(status: str) -> None:
    result = client(Transport(fixture(status, expiry=None))).retrieve(REF)
    assert result.status == status
    assert result.capture_before is None
    assert result.authorization_inspection is None


def refund_fixture(
    status: str = "succeeded", amount: int = 2999, ref: str = "re_Original"
) -> dict[str, object]:
    return {
        "object": "refund",
        "id": ref,
        "charge": CHARGE,
        "payment_intent": REF,
        "amount": amount,
        "currency": "usd",
        "status": status,
        "metadata": {"private": "PRIVATE REFUND"},
        "failure_reason": "PRIVATE REFUND",
    }


def refund_page(*refunds: dict[str, object], more: bool = False) -> dict[str, object]:
    return {"object": "list", "data": list(refunds), "has_more": more, "url": "/v1/refunds"}


def test_sdk_original_full_refund_request_and_authoritative_lookup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = Transport(refund_fixture(), fixture("succeeded"), refund_page(refund_fixture()))
    adapter = client(transport)
    with caplog.at_level(logging.DEBUG):
        response = adapter.refund(
            REF, CHARGE, 2999, "immutable-refund-key", captured_cents=CERTIFIED_CENTS
        )
        assert response.refunds is not None
        reconciled = adapter.retrieve_refunds(REF, CHARGE)
    assert reconciled.full_refund
    assert transport.calls == [
        (
            "post",
            "/v1/refunds",
            {"charge": [CHARGE], "amount": ["2999"]},
            "immutable-refund-key",
            VERSION,
        ),
        ("get", f"/v1/payment_intents/{REF}", {"expand[0]": ["latest_charge"]}, None, VERSION),
        ("get", "/v1/refunds", {"charge": [CHARGE], "limit": ["100"]}, None, VERSION),
    ]
    assert "PRIVATE" not in caplog.text
    assert "offline-placeholder" not in caplog.text


def test_sdk_refund_pagination_complete_dedup_exact_total() -> None:
    first = refund_fixture(amount=1999, ref="re_First")
    last = refund_fixture(amount=1000, ref="re_Last")
    transport = Transport(
        fixture("succeeded"), refund_page(first, more=True), refund_page(first, last)
    )
    outcome = client(transport).retrieve_refunds(REF, CHARGE)
    assert outcome.full_refund
    assert outcome.refunds is not None
    assert len(outcome.refunds) == 2
    assert transport.calls[-1][2] == {
        "charge": [CHARGE],
        "limit": ["100"],
        "starting_after": ["re_First"],
    }


@pytest.mark.parametrize(
    "mistake",
    [
        "wrongcharge",
        "wrongpi",
        "currency",
        "amount",
        "status",
        "malformed",
        "incomplete",
        "duplicate",
        "lookupfail",
    ],
)
def test_sdk_refund_bad_evidence_stays_uncertain(mistake: str) -> None:
    refund = refund_fixture()
    page = refund_page(refund)
    if mistake == "wrongcharge":
        refund["charge"] = "ch_Other"
    elif mistake == "wrongpi":
        refund["payment_intent"] = "pi_Other"
    elif mistake == "currency":
        refund["currency"] = "eur"
    elif mistake == "amount":
        refund["amount"] = 3000
    elif mistake == "status":
        refund["status"] = "unknown"
    elif mistake == "malformed":
        page["has_more"] = 0
    elif mistake == "incomplete":
        page = refund_page(more=True)
    elif mistake == "duplicate":
        page = refund_page(refund, refund_fixture(amount=1))
    response: object = TimeoutError() if mistake == "lookupfail" else page
    outcome = client(Transport(fixture("succeeded"), response)).retrieve_refunds(REF, CHARGE)
    assert outcome.status == "uncertain"
    assert outcome.refunds is None


@pytest.mark.parametrize("status", ["pending", "failed", "requires_action"])
def test_sdk_refund_noncompletion_statuses(status: str) -> None:
    outcome = client(
        Transport(fixture("succeeded"), refund_page(refund_fixture(status)))
    ).retrieve_refunds(REF, CHARGE)
    assert outcome.status == "succeeded"  # Captured money truth, not refund success.
    assert not outcome.full_refund
    assert outcome.refundable is (status == "failed")


def test_sdk_refund_cached500_no_retry_and_later_full_reconciliation(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        store.save_workspace("workspace", Workspace(), NOW)
        case = letter(
            store,
            store.start_case("workspace", REF, NOW, datetime.fromtimestamp(EXPIRY, UTC)),
            cs.WindowExpired(),
        )
        cursor = step(store, client(Transport(fixture("succeeded"))), case, compensation=True)
        cursor = step(store, client(Transport(fixture("succeeded"), refund_page())), case, cursor)
        error_transport = Transport({"error": {"message": "PRIVATE"}}, status=500)
        cursor = step(store, client(error_transport), case, cursor)
        assert cursor.outcome.status == "uncertain"
        assert len(error_transport.calls) == 1
        old_key = cursor.mutation_key
        lookup = Transport(fixture("succeeded"), refund_page(refund_fixture()))
        cursor = step(store, client(lookup), case, cursor, at=NOW + timedelta(days=2))
        assert cursor.done
        assert cursor.mutation_key == old_key
        assert all(c[0] == "get" for c in lookup.calls)


def test_amount_refunded_is_not_success_and_invalid_refund_amount_is_refused() -> None:
    obj = fixture("succeeded")
    charge(obj)["amount_refunded"] = 2999
    adapter = client(Transport(obj, refund_page(refund_fixture("pending"))))
    outcome = adapter.retrieve_refunds(REF, CHARGE)
    assert not outcome.full_refund
    assert not outcome.refundable
    # T30 (2026-10-21): exact partial returned-mail refunds (1..captured) are now valid; amounts
    # outside that range, or of the wrong type, are still refused before any provider request.
    for amount in (0, -1, 3000, True, 1950.0):
        transport = Transport()
        with pytest.raises(ValueError, match="within captured service amount"):
            client(transport).refund(
                REF,
                CHARGE,
                amount,  # type: ignore[arg-type]
                "key",
                captured_cents=CERTIFIED_CENTS,
            )
        assert transport.calls == []


def test_refund_page_failure_or_no_progress_is_not_absence() -> None:
    first = refund_fixture("failed")
    for response in (TimeoutError(), refund_page(first, more=True)):
        outcome = client(
            Transport(fixture("succeeded"), refund_page(first, more=True), response)
        ).retrieve_refunds(REF, CHARGE)
        assert outcome.status == "uncertain"
        assert outcome.refunds is None


def test_authorize_serializes_manual_confirmed_card_hold_and_parses_evidence() -> None:
    transport = Transport(fixture())
    adapter: PaymentClient = client(transport)
    result = adapter.authorize(2999, "pm_card_visa", "intake-key", {"submission_token": "opaque"})
    assert result.status == "requires_capture"
    assert result.authorization_inspection is not None
    assert result.authorization_ref == REF
    assert transport.calls == [
        (
            "post",
            "/v1/payment_intents",
            {
                "amount": ["2999"],
                "currency": ["usd"],
                "capture_method": ["manual"],
                "confirm": ["true"],
                "payment_method": ["pm_card_visa"],
                "automatic_payment_methods[enabled]": ["true"],
                "automatic_payment_methods[allow_redirects]": ["never"],
                "metadata[submission_token]": ["opaque"],
                "expand[0]": ["latest_charge"],
            },
            "intake-key",
            VERSION,
        )
    ]


@pytest.mark.parametrize("amount", [0, 3000, True])
def test_authorize_rejects_non_service_amount_without_transport(amount: int) -> None:
    transport = Transport()
    with pytest.raises(ValueError, match="full service authorization"):
        client(transport).authorize(amount, "pm_card_visa", "key", {})
    assert not transport.calls


@pytest.mark.parametrize(("method", "key"), [("", "key"), ("pm_", "key"), ("pm_card_visa", " ")])
def test_authorize_rejects_invalid_inputs_without_transport(method: str, key: str) -> None:
    transport = Transport()
    with pytest.raises(ValueError, match="required"):
        client(transport).authorize(2999, method, key, {})
    assert not transport.calls


@pytest.mark.parametrize(
    "response", [RuntimeError("private provider detail"), {}, fixture(expiry=None)]
)
def test_authorize_uncertainty_has_no_exception_details(response: object) -> None:
    transport = Transport(response)
    result = client(transport).authorize(2999, "pm_card_visa", "key", {})
    assert result.status == "uncertain"
    assert "private" not in repr(result)
    assert len(transport.calls) == 1


def test_fake_authorize_caches_and_checks_idempotency_parameters() -> None:
    adapter: PaymentClient = FakePaymentClient()
    held = adapter.authorize(2999, "pm_card_visa", "key", {"smoke": "token"})
    assert held.status == "requires_capture"
    assert adapter.authorize(2999, "pm_card_visa", "key", {"smoke": "token"}) == held
    with pytest.raises(ValueError, match="idempotency parameters"):
        adapter.authorize(2999, "pm_other", "key", {"smoke": "token"})


def test_fake_stripe_transport_forbids_multicapture_final_capture_parameter() -> None:
    from parkingbreaker.config_fakes import FakeStripeTransport
    from parkingbreaker.stripe_payment import STRIPE_API_VERSION

    fake_transport = FakeStripeTransport()
    adapter = StripePaymentClient(
        secret_key="sk_test_offline",
        api_version=STRIPE_API_VERSION,
        transport=fake_transport,
    )
    held = adapter.authorize(2999, "pm_card_visa", "auth-key", {})
    assert held.status == "requires_capture"
    _, bad_status, _ = fake_transport.request(
        "post",
        f"https://api.stripe.com/v1/payment_intents/{held.authorization_ref}/capture",
        {"Idempotency-Key": "bad-multicapture-key"},
        "amount_to_capture=2999&final_capture=true&expand%5B0%5D=latest_charge",
    )
    assert bad_status == 400
    captured = adapter.capture(held.authorization_ref, 2999, "cap-key", final_capture=True)
    assert captured.status == "succeeded"
    assert captured.amount_received == 2999
