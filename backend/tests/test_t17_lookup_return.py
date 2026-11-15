"""Exact lookup return evidence must survive atomic acceptance and process restarts."""

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from parkingbreaker.case_state import CaseState
from parkingbreaker.lob_submission import Accepted, LetterEvidence, UspsScan
from parkingbreaker.lob_webhook import has_returned_scan
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_submission_coordinator import NOW, prepared
from test_t17_capture_policy import run_dispatches
from test_t17_mail_evidence import (
    OfflineLob,
    current_case,
    cursor_for,
    list_response,
    new_id,
    resource,
    submit,
)


@pytest.mark.parametrize(
    ("kind", "name", "expected"),
    [
        ("normal", "Returned to Sender", True),
        ("certified", "Returned to Sender", True),
        ("return_envelope", "Returned to Sender", False),
        ("unknown", "Returned to Sender", False),
        ("normal", "Delivered", False),
        ("normal", "Mailed", False),
    ],
)
def test_only_original_carrier_return_scans_qualify(kind: str, name: str, expected: bool) -> None:
    assert has_returned_scan((UspsScan(kind, name, None, None),)) is expected
    assert has_returned_scan(()) is False


@pytest.mark.parametrize("kind", ["normal", "certified"])
@pytest.mark.parametrize("late", [False, True], ids=["within_hold", "after_expiry"])
def test_lookup_return_applies_once_across_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, late: bool
) -> None:
    path = tmp_path / "lookup-return.sqlite"
    wire = OfflineLob(monkeypatch, lambda *_: (200, resource()))
    payments = FakePaymentClient()
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        submit(store, wire, case, NOW)
    at = case.authorization_expires_at + timedelta(minutes=1) if late else NOW + timedelta(hours=23)
    scan = {
        "type": kind,
        "name": "Returned to Sender",
        "time": (at - timedelta(minutes=5)).isoformat(),
        "details": {"event": "No Such Number"},
    }
    body = resource(tracking_events=[scan])
    wire.respond = lambda _method, route: list_response([body]) if "?" in route else (200, body)
    run_dispatches(path, wire, payments, at)
    run_dispatches(path, wire, payments, at + timedelta(days=2))
    with SqliteTransactionStore(path) as store:
        assert current_case(store, case).letter_state is CaseState.RETURNED
        assert isinstance(cursor_for(store, case).outcome, Accepted)
        scans = store.usps_scans(case.case_token)
        assert len(scans) == 1
        assert scans[0].as_sent() == scan
        history = store.transition_history(case.case_token)
        assert sum(row.event_name == "SubmitConfirmed" for row in history) == 1
        assert sum(row.event_name == "ReturnConfirmed" for row in history) == 1
        assert next(row for row in history if row.event_name == "ReturnConfirmed").occurred_at == at
    assert payments.captures == (0 if late else 1)
    assert payments.releases == 0
    assert len(wire.creates) == 1


def test_return_acceptance_rolls_back_with_cursor_completion_and_recovers(tmp_path: Path) -> None:
    path = tmp_path / "atomic-return.sqlite"
    outcome = Accepted(
        "ltr_returned",
        evidence=LetterEvidence.PRODUCTION,
        scans=(UspsScan("normal", "Returned to Sender", NOW.isoformat(), {"event": "Other"}),),
    )
    with SqliteTransactionStore(path) as store, store.submission_ownership():
        case = prepared(store)
        claimed, _ = store.claim_submission(
            case.case_token, 1, case.revision, None, now=NOW, operation_id="claim-return"
        )
        history = store.transition_history(case.case_token)
        effects = store.effects(case.case_token)
        store._require_open().execute(
            "CREATE TRIGGER fail_return_completion BEFORE UPDATE ON pb_submission_cursors "
            "WHEN NEW.owner IS NULL BEGIN SELECT RAISE(ABORT, 'injected completion crash'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected completion crash"):
            store.finish_submission(claimed, outcome, now=NOW, effect_ids=(), new_effect_id=new_id)
        assert store.transition_history(case.case_token) == history
        assert store.effects(case.case_token) == effects
        assert store.submission_cursor(case.case_token, 1) == claimed
        assert store._require_open().execute(
            "SELECT outcome_json, completed_at FROM pb_submission_operations WHERE operation_id=?",
            (claimed.owner,),
        ).fetchone() == (None, None)
    with SqliteTransactionStore(path) as store, store.submission_ownership():
        store._require_open().execute("DROP TRIGGER fail_return_completion")
        store.finish_submission(claimed, outcome, now=NOW, effect_ids=(), new_effect_id=new_id)
        assert current_case(store, case).letter_state is CaseState.RETURNED
        assert (
            sum(
                row.event_name == "ReturnConfirmed"
                for row in store.transition_history(case.case_token)
            )
            == 1
        )
        assert store.usps_scans(case.case_token) == outcome.scans
