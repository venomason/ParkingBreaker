"""The operator report reads the same durable records used for settlement."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.paid_beta_report import main, paid_beta_report
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.payment_state import CaptureFailed, ReconciliationExhausted, RefundIssued
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_case_service import fake_resolution as fake_resolution
from test_payment_coordinator import step
from test_t14_authorization_recovery import BODY, RecoveryCase
from test_t14_authorization_recovery import recovery as recovery
from test_transaction_lifecycle import NOW, mailed, payment


def test_paid_report_preserves_holds_partial_refunds_and_cohort_boundary(tmp_path: Path) -> None:
    path = tmp_path / "cohort & receipts.sqlite"
    as_of = NOW + timedelta(days=1)
    with SqliteTransactionStore(path) as store:
        for name, authorized, expires in (
            ("older", NOW - timedelta(microseconds=1), as_of + timedelta(hours=1)),
            ("expiring", NOW, as_of + timedelta(hours=1)),
            ("expired", NOW, as_of),
        ):
            store.save_workspace(name, Workspace(notes="PRIVATE CUSTOMER NOTE"), authorized)
            store.start_case(name, f"provider-{name}", authorized, expires)
        case = mailed(store)
        queued = paid_beta_report(path, since=NOW, now=as_of)
        assert queued["cases_with_pending_payment_work"] == 1
        assert queued["recorded_gross_capture_cents"] == 0
        stripe = FakePaymentClient()
        attempt = step(store, stripe, case)
        assert step(store, stripe, case, attempt).done
        captured = store.get_case(case.case_token)
        assert captured is not None
        store.apply_payment_event(
            captured.case_token,
            RefundIssued("private-refund-ref", 1200, captured.amount_cents),
            captured.revision,
            occurred_at=NOW,
        )
        before = store.transition_history(case.case_token)
        report = paid_beta_report(path, since=NOW, now=as_of)
        assert store.transition_history(case.case_token) == before
    assert report["paid_cases"] == 3
    assert report["payment_states"] == {"AUTHORIZED": 2, "REFUNDED": 1}
    assert report["recorded_gross_capture_cents"] == 2999
    assert report["recorded_refund_cents"] == 1200
    assert report["capture_outcome_unknown"] == report["refund_amount_unknown"] == 0
    assert report["holds_expiring_within_24h"] == report["expired_holds"] == 1
    serialized = json.dumps(report)
    restricted = (
        "PRIVATE CUSTOMER NOTE",
        "private-refund-ref",
        "provider-expired",
        case.case_token,
    )
    assert all(value not in serialized for value in restricted)


def test_recent_cohort_includes_older_unresolved_payment_attempt(recovery: RecoveryCase) -> None:
    recovery.payments.lose_next_response = True
    with pytest.raises(ServiceConflict, match="card_not_authorized"):
        recovery.real.authorize(recovery.case_id, BODY)
    since = recovery.clock() + timedelta(hours=1)
    report = paid_beta_report(recovery.path, since=since, now=since + timedelta(hours=1))
    assert report["paid_cases"] == report["recorded_gross_capture_cents"] == 0
    assert report["outstanding_pre_case_attempts_all_cohorts"] == {"pending": 0, "uncertain": 1}


def test_unreconciled_capture_is_visible_after_write_off(tmp_path: Path) -> None:
    path = tmp_path / "writeoff.sqlite"
    with SqliteTransactionStore(path) as store:
        case = mailed(store)
        case = payment(store, case, CaptureFailed("unresolved"))
        payment(store, case, ReconciliationExhausted())
    report = paid_beta_report(path, since=NOW, now=NOW + timedelta(hours=1))
    assert report["payment_states"] == {"WRITTEN_OFF": 1}
    assert report["capture_outcome_unknown"] == 1
    assert report["recorded_gross_capture_cents"] == 0


def test_hold_warning_counts_elapsed_hours_across_spring_forward(tmp_path: Path) -> None:
    path = tmp_path / "spring-forward.sqlite"
    now = datetime(2026, 3, 7, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
    since = now - timedelta(hours=1)
    with SqliteTransactionStore(path) as store:
        store.save_workspace("workspace", Workspace(), since)
        expiry = now.astimezone(UTC) + timedelta(hours=23, minutes=30)
        store.start_case("workspace", "auth", since, expiry)
    report = paid_beta_report(path, since=since, now=now)
    assert report["holds_expiring_within_24h"] == 1


def test_cli_refuses_missing_database_without_creating_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "missing.sqlite"
    assert main([str(path), "--since", NOW.isoformat()]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"
    assert not path.exists()


def test_first_twenty_live_captures_stay_fixed_as_results_arrive(tmp_path: Path) -> None:
    from typing import Any, cast

    path = tmp_path / "review.sqlite"
    with SqliteTransactionStore(path) as store:
        db = store._require_open()

        def capture(index: int, mode: bool | None, basis: str, *, charge: str | None = None) -> str:
            workspace = f"synthetic-{index}"
            store.save_workspace(workspace, Workspace(), NOW)
            case = store.start_case(workspace, f"pi_Synthetic{index}", NOW, NOW + timedelta(days=1))
            charge = charge or f"ch_Synthetic{index}"
            db.execute(
                "INSERT INTO pb_case_history VALUES (?, 2, 'payment', 'CAPTURE_PENDING', "
                "'CAPTURED', 'CaptureSucceeded', ?, ?)",
                (case.case_token, json.dumps({"charge_ref": charge}), NOW.isoformat()),
            )
            db.execute(
                "INSERT INTO pb_original_capture_basis VALUES (?, ?)", (case.case_token, basis)
            )
            if mode is not None:
                result = {
                    "outcome": {"status": "succeeded", "charge_ref": charge, "livemode": mode}
                }
                db.execute(
                    "INSERT INTO pb_payment_operations VALUES (?, ?, '{}', ?, ?)",
                    (workspace, case.case_token, json.dumps(result), NOW.isoformat()),
                )
            return case.case_token

        capture(0, False, "usps_scan")
        capture(1, None, "unknown")
        for index in range(2, 21):
            capture(index, True, "usps_scan")
        as_of = NOW + timedelta(days=1)
        before = cast(dict[str, Any], paid_beta_report(path, since=NOW, now=as_of))
        assert not before["original_capture_review_all_dates"]["first_20_live_review"]["review_due"]
        twentieth = capture(21, True, "hold_expiry_fallback")
        view = cast(dict[str, Any], paid_beta_report(path, since=NOW, now=as_of))
        review = view["original_capture_review_all_dates"]["first_20_live_review"]
        assert review["review_due"]
        assert review["successful_original_captures"] == 20
        assert review["hold_expiry_fallback_captures"] == 1
        assert review["hold_expiry_fallback_share"] == 0.05
        capture(22, True, "hold_expiry_fallback")
        capture(23, True, "hold_expiry_fallback", charge="ch_Synthetic21")
        db.execute(
            "INSERT INTO pb_case_history VALUES (?, 3, 'payment', 'CAPTURED', "
            "'REFUNDED', 'RefundIssued', ?, ?)",
            (
                twentieth,
                json.dumps({"amount_cents": 2999, "captured_cents": 2999}),
                NOW.isoformat(),
            ),
        )
    updated = cast(dict[str, Any], paid_beta_report(path, since=NOW, now=as_of))
    overall = updated["original_capture_review_all_dates"]
    revised = overall["first_20_live_review"]
    assert revised["review_due"]
    assert revised["hold_expiry_fallback_captures"] == 1
    assert revised["full_refund_confirmed"] == 1
    assert [r["capture_number"] for r in revised["captures"]] == [
        r["capture_number"] for r in review["captures"]
    ]
    assert overall["successful_original_captures"] == 23
    assert overall["hold_expiry_fallback_captures"] == 2
    assert overall["unknown_capture_basis"] == 1
    assert overall["by_environment"]["test"]["successful_original_captures"] == 1
    assert overall["by_environment"]["unknown"]["successful_original_captures"] == 1
