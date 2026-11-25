"""Offline capture, private evidence, signed provider ingress and full original refund."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.paid_beta_report import paid_beta_report
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.payment_records import RefundEvidence
from parkingbreaker.retention_policy import DataScope
from parkingbreaker.retention_state import initialize_retention_state, record_hold, release_hold
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.verify_city_mail import record_verified_city
from test_case_service import service
from test_stripe_payment import CHARGE, REF, Transport, client, fixture
from test_submission_coordinator import NOW
from test_t17_capture_policy import fallback_world, run_dispatches, scan, signed_event
from test_t17_mail_evidence import (
    ORIGINAL,
    OfflineLob,
    clock_at,
    current_case,
    new_id,
    resource,
)

type PaidWorld = tuple[OfflineLob, FakePaymentClient, Path, CaseRecord, datetime, dict[str, object]]


@pytest.fixture
def paid_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PaidWorld:
    payload = resource(
        send_date=(NOW - timedelta(hours=1)).isoformat(),
        url="https://offline.example/private-proof",
        description="PRIVATE SYNTHETIC MAIL CONTENT",
    )
    wire, _, path, case, now = fallback_world(tmp_path, monkeypatch, payload)
    payments = FakePaymentClient("auth")
    payments.responses = [
        replace(
            client(Transport(fixture("succeeded"))).retrieve(REF),
            authorization_ref="auth",
            livemode=True,
        )
    ]
    return wire, payments, path, case, now, payload


def test_atomic_exact_response_basis_and_retention(
    paid_world: PaidWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire, payments, path, case, now, payload = paid_world
    with SqliteTransactionStore(path) as store:
        original = store._apply_event

        def interrupt(case_id: str, event: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(event, cs.SubmitConfirmed):
                raise RuntimeError("synthetic transaction abort")
            return original(case_id, event, *args, **kwargs)

        monkeypatch.setattr(store, "_apply_event", interrupt)
        with pytest.raises(RuntimeError, match="synthetic transaction abort"):
            dispatch_once(store, wire.client, payments, clock_at(now), new_id)
        assert store._require_open().execute(
            "SELECT COUNT(*) FROM pb_capture_fallback_evidence"
        ).fetchone() == (0,)
        manifest = store.get_approval_manifest(case.case_token, 1)
        assert manifest is not None
        assert manifest.dispatched_pdf_sha256 is None
        assert current_case(store, case).payment_state is ps.PaymentState.AUTHORIZED
    run_dispatches(path, wire, payments, now)
    raw = json.dumps(payload).encode()
    with SqliteTransactionStore(path) as store:
        db = store._require_open()
        assert db.execute(
            "SELECT letter_id, send_date, observed_at, raw_sha256, raw_body "
            "FROM pb_capture_fallback_evidence"
        ).fetchone() == (
            ORIGINAL,
            payload["send_date"],
            now.isoformat(),
            hashlib.sha256(raw).hexdigest(),
            raw,
        )
        assert not path.stat().st_mode & 0o077
        assert store.original_capture_basis(case.case_token) == "hold_expiry_fallback"
        assert "PRIVATE SYNTHETIC" not in str(store.submission_cursor(case.case_token, 1))
        assert (
            "PRIVATE SYNTHETIC"
            not in db.execute(
                "SELECT group_concat(outcome_json) FROM pb_submission_operations"
            ).fetchone()[0]
        )
        initialize_retention_state(db)
        record_hold(
            db,
            command_id="assert-hold",
            case_token=case.case_token,
            hold_id="proof-hold",
            scope=DataScope.FINANCIAL_PROVIDER_DATA,
            source_ref="synthetic-review",
            at=now,
        )
        expiration = now + timedelta(days=90)
        store.expire_raw_fallback_lookups(expiration)
        assert db.execute("SELECT raw_body FROM pb_capture_fallback_evidence").fetchone() == (raw,)
        release_hold(
            db,
            command_id="release-hold",
            case_token=case.case_token,
            hold_id="proof-hold",
            at=expiration,
            expected_generation=1,
        )
        store.expire_raw_fallback_lookups(expiration)
        assert db.execute(
            "SELECT raw_sha256, raw_body FROM pb_capture_fallback_evidence"
        ).fetchone() == (hashlib.sha256(raw).hexdigest(), None)
    assert payments.captures == 1


def send_lob_event(
    path: Path,
    payments: FakePaymentClient,
    now: datetime,
    event: str,
    obj: dict[str, object],
    *,
    event_id: str,
) -> bool:
    api = service(path, payments, clock_at(now))
    object.__setattr__(api._deps, "lob_webhook_secret", "secret")
    object.__setattr__(api._deps, "new_id", new_id)
    payload, headers = signed_event(obj, event, now, event_id=event_id)
    try:
        return api.webhook("lob", payload, headers)
    finally:
        api._store.close()


@pytest.mark.parametrize("external_partial", [0, 1200])
def test_exact_signed_deletion_refunds_original_once_after_restart(
    paid_world: PaidWorld, external_partial: int
) -> None:
    wire, payments, path, case, now, _ = paid_world
    run_dispatches(path, wire, payments, now)
    if external_partial:
        payments.refunds = (
            RefundEvidence("re_External", "auth", CHARGE, external_partial, "usd", "succeeded"),
        )
    observed = now + timedelta(hours=1)
    deletion = resource(deleted=True)
    assert send_lob_event(
        path, payments, observed, "letter.deleted", deletion, event_id="evt_delete"
    )
    assert not send_lob_event(
        path, payments, observed, "letter.deleted", deletion, event_id="evt_delete"
    )
    with SqliteTransactionStore(path) as store:
        assert store.nonmailing_refund_status(case.case_token) == "pending"
        assert current_case(store, case).payment_state is ps.PaymentState.CAPTURED
    for offset in (0, 30, 60, 90):
        run_dispatches(path, wire, payments, observed + timedelta(minutes=offset))
    with SqliteTransactionStore(path) as store:
        assert current_case(store, case).payment_state is ps.PaymentState.REFUNDED
        assert store.nonmailing_refund_status(case.case_token) == "refunded"
        cursor = store.payment_cursor(case.case_token)
        assert cursor is not None
        assert cursor.done
        assert store.original_capture_basis(case.case_token) == "hold_expiry_fallback"
    assert payments.captures == payments.refund_calls == 1
    assert sum(r.amount_cents for r in payments.refunds) == case.amount_cents
    report = paid_beta_report(path, since=observed, now=observed + timedelta(days=1))
    original = cast(dict[str, Any], report["original_capture_review_all_dates"])
    assert original["successful_original_captures"] == 1
    assert original["hold_expiry_fallback_captures"] == 1
    assert original["full_refund_confirmed"] == 1
    assert original["fallback_outcomes"][0]["never_mailed_confirmed"]
    assert original["by_environment"]["live"]["successful_original_captures"] == 1
    for private in (ORIGINAL, CHARGE, case.case_token, "private-proof", "re_External"):
        assert private not in json.dumps(report)


@pytest.mark.parametrize(
    "evidence", ["failure", "return", "wrong_identity", "late_scan", "printer_handoff", "unsigned"]
)
def test_ambiguous_and_contradictory_evidence_cannot_refund(
    paid_world: PaidWorld, evidence: str
) -> None:
    wire, payments, path, case, now, _ = paid_world
    run_dispatches(path, wire, payments, now)
    observed = now + timedelta(hours=1)
    if evidence == "failure":
        send_lob_event(
            path,
            payments,
            observed,
            "letter.failed",
            resource(status="failed"),
            event_id="evt_failure",
        )
    elif evidence == "wrong_identity":
        send_lob_event(
            path,
            payments,
            observed,
            "letter.deleted",
            resource(deleted=True, metadata={"submission_token": "wrong"}),
            event_id="evt_wrong",
        )
    elif evidence == "return":
        send_lob_event(
            path, payments, observed, "letter.returned_to_sender", resource(), event_id="evt_return"
        )
    elif evidence == "late_scan":
        send_lob_event(
            path,
            payments,
            observed,
            "letter.deleted",
            resource(deleted=True),
            event_id="evt_deletion",
        )
        run_dispatches(path, wire, payments, observed, 1)
        send_lob_event(
            path,
            payments,
            observed,
            "letter.in_transit",
            resource(tracking_events=scan()),
            event_id="evt_scan",
        )
    elif evidence == "printer_handoff":
        assert send_lob_event(
            path, payments, observed, "letter.mailed", resource(), event_id="evt_handoff"
        )
        assert send_lob_event(
            path,
            payments,
            observed,
            "letter.deleted",
            resource(deleted=True),
            event_id="evt_deletion",
        )
    else:
        api = service(path, payments, clock_at(observed))
        object.__setattr__(api._deps, "lob_webhook_secret", "secret")
        body, _ = signed_event(resource(deleted=True), "letter.deleted", observed)
        try:
            with pytest.raises(ServiceConflict):
                api.webhook("lob", body, {})
        finally:
            api._store.close()
    run_dispatches(path, wire, payments, observed)
    with SqliteTransactionStore(path) as store:
        assert current_case(store, case).payment_state is ps.PaymentState.CAPTURED
        assert store.original_capture_basis(case.case_token) == "hold_expiry_fallback"
        if evidence in {"late_scan", "printer_handoff"}:
            assert store.nonmailing_refund_status(case.case_token) == "evidence_conflict"
    assert payments.refund_calls == 0


@pytest.mark.parametrize("found", ["deleted", "scan", "missing"])
def test_recheck_recovers_missed_provider_update(paid_world: PaidWorld, found: str) -> None:
    wire, payments, path, case, now, _ = paid_world
    run_dispatches(path, wire, payments, now)
    wire.respond = lambda *_: {
        "deleted": (200, resource(deleted=True)),
        "scan": (200, resource(tracking_events=scan())),
        "missing": (404, {"error": {"code": "not_found"}}),
    }[found]
    run_dispatches(path, wire, payments, now + timedelta(days=1))
    with SqliteTransactionStore(path) as store:
        facts = store.original_mail_facts(case.case_token)
        assert ("usps_scan" in facts) is (found == "scan")
        assert ("never_mailed" in facts) is (found == "deleted")
        assert store.original_capture_basis(case.case_token) == "hold_expiry_fallback"
    assert payments.refund_calls == (1 if found == "deleted" else 0)


@pytest.mark.parametrize("finding", ["city_receipt", "never_mailed"])
def test_verified_city_ingress_uses_private_evidence_and_exact_letter(
    paid_world: PaidWorld, tmp_path: Path, finding: str
) -> None:
    wire, payments, path, case, now, _ = paid_world
    run_dispatches(path, wire, payments, now)
    evidence = tmp_path / "synthetic-city-record"
    evidence.write_text("synthetic verified city record")
    evidence.chmod(0o644)
    with pytest.raises(ValueError, match="private file"):
        record_verified_city(path, evidence, case.case_token, ORIGINAL, finding)
    evidence.chmod(0o600)
    with pytest.raises(ValueError, match="exact original"):
        record_verified_city(path, evidence, case.case_token, "ltr_Other", finding)
    assert record_verified_city(path, evidence, case.case_token, ORIGINAL, finding)
    assert not record_verified_city(path, evidence, case.case_token, ORIGINAL, finding)
    run_dispatches(path, wire, payments, max(now, datetime.now(UTC)) + timedelta(seconds=1))
    assert payments.refund_calls == (1 if finding == "never_mailed" else 0)
    with SqliteTransactionStore(path) as store:
        assert finding in store.original_mail_facts(case.case_token)
        assert store.original_capture_basis(case.case_token) == "hold_expiry_fallback"


def test_refund_response_loss_reconciles_the_same_original_charge(
    paid_world: PaidWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire, payments, path, case, now, _ = paid_world
    run_dispatches(path, wire, payments, now)
    observed = now + timedelta(hours=1)
    assert send_lob_event(
        path, payments, observed, "letter.deleted", resource(deleted=True), event_id="evt_delete"
    )
    refund = payments.refund

    def lose_response(*args: Any, **kwargs: Any) -> Any:
        refund(*args, **kwargs)
        raise TimeoutError("synthetic response lost after provider accepted refund")

    monkeypatch.setattr(payments, "refund", lose_response)
    run_dispatches(path, wire, payments, observed)
    assert payments.refund_calls == 1
    with SqliteTransactionStore(path) as store:
        assert store.nonmailing_refund_status(case.case_token) == "pending"
    run_dispatches(path, wire, payments, observed + timedelta(minutes=30))
    with SqliteTransactionStore(path) as store:
        assert store.nonmailing_refund_status(case.case_token) == "refunded"
    assert payments.refund_calls == 1
