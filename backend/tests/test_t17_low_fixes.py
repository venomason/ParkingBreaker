"""Regression tests for the four LOW findings of the T17 piece B final review.

Each test reproduces its probe from build-kit/evidence/task-T17-2026-10-21/
final-review-pieceB-opus.md and fails on the reviewed code: a local unpaid re-mail
refusal keeps its own reason and customer copy instead of blaming the mail provider,
an identity-mismatched return never shadows the genuine return evidence or the
return proof packet, a still-capturable payment never gets the final "no charge"
wording, and the docs keep the retained scans for a future delivery report.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from mail_samples import make_request, seed_approval, seed_destination
from parkingbreaker import case_state as cs
from parkingbreaker.address_policy import Responsibility, ResponsibilityDecision
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.lob_submission import Rejected
from parkingbreaker.payment_fakes import FakePaymentClient, full_outcome
from parkingbreaker.payment_records import PaymentOutcome
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.returned_mail import ReturnCause
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_case_service import service
from test_submission_coordinator import NOW
from test_t17_capture_policy import run_dispatches, scan
from test_t17_mail_evidence import (
    ORIGINAL,
    OfflineLob,
    current_case,
    list_response,
    new_id,
    resource,
    submit,
)
from test_t17_pieceB_review import (
    app_for,
    distinct_event,
    remail_lob,
    returned_with_remail_intent,
)
from test_transaction_lifecycle import letter

# --- LOW-1: the unpaid re-mail refusal keeps its own reason and copy -----------------


def test_unpaid_remail_refusal_keeps_its_reason_and_customer_copy(tmp_path: Path) -> None:
    """A local unpaid refusal is never presented as a mail provider rejection (H2)."""
    path = tmp_path / "cases.sqlite"
    lob = remail_lob()
    payments = FakePaymentClient()
    case, late = returned_with_remail_intent(path, lob, expired=True)
    payments.object = full_outcome(PaymentOutcome("canceled", case.authorization_ref))
    for _ in range(4):
        with SqliteTransactionStore(path) as store:
            dispatch_once(store, lob, payments, lambda: late, new_id)
    with SqliteTransactionStore(path) as store:
        saved = current_case(store, case)
        cursor = store.submission_cursor(case.case_token, 2)
    assert cursor is not None
    assert isinstance(cursor.outcome, Rejected)
    assert cursor.outcome.reason == "ORIGINAL_PAYMENT_NOT_CAPTURED"
    app = service(path, payments)
    try:
        copy, active = app._remail_outcome_copy(saved)
        remedy = app._return_remedy_status(saved, "probe")
    finally:
        app._store.close()
    assert copy == "The free re-mail was not sent because no original payment was captured."
    assert active is False
    assert remedy is not None
    assert remedy.message == (
        "Your letter was returned. The free re-mail was not sent because no original "
        "payment was captured. No charge was made."
    )
    assert "mail provider rejected" not in remedy.message


# --- LOW-2: a mismatched return shadows neither evidence nor proof ------------------


def _bound_prepared(store: SqliteTransactionStore) -> CaseRecord:
    """prepared() with the official destination bound into the frozen request.

    The binding lets classify_return see the saved official address, so the probe's
    REFUSED return classifies as agency-side and its cause is observable evidence.
    """
    store.save_workspace("workspace", Workspace(), NOW)
    case = store.start_case("workspace", "auth", NOW, NOW + timedelta(days=5))
    for event in (
        cs.ResearchComplete(),
        cs.DraftReady(),
        cs.CustomerApproved(),
        cs.SubmitRequested(),
    ):
        case = letter(store, case, event)
    seed_destination(store, case, NOW)
    store.prepare(case.case_token, 1, make_request(), NOW)
    seed_approval(store, case.case_token)
    return case


def test_mismatched_return_shadows_neither_evidence_nor_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The genuine REFUSED return keeps its cost estimate through a later mismatch."""
    path = tmp_path / "cases.sqlite"
    wire = OfflineLob(monkeypatch, lambda *_: (200, resource()))
    payments = FakePaymentClient()
    with SqliteTransactionStore(path) as store:
        case = _bound_prepared(store)
        submit(store, wire, case, NOW)
    run_dispatches(path, wire, payments, NOW, 1)
    accepted = resource(tracking_events=scan())
    wire.respond = lambda _method, route: (
        list_response([accepted]) if "?" in route else (200, accepted)
    )
    run_dispatches(path, wire, payments, NOW + timedelta(hours=23), 2)
    genuine = resource(
        tracking_events=[
            {
                "type": "normal",
                "name": "Returned to Sender",
                "time": NOW.isoformat(),
                "details": {"event": "Refused"},
            }
        ]
    )
    conflicting = dict(genuine, metadata={"submission_token": "someone-else"})
    times = (NOW + timedelta(hours=23), NOW + timedelta(hours=24))
    for (event_id, body), at in zip(
        (("genuine", genuine), ("conflicting", conflicting)), times, strict=True
    ):
        app = app_for(path, payments, at)
        payload, headers = distinct_event(body, "letter.returned_to_sender", at, event_id)
        try:
            assert app.webhook("lob", payload, headers)
        finally:
            app._store.close()
    with SqliteTransactionStore(path) as store:
        saved = current_case(store, case)
        assert saved.letter_state is cs.CaseState.RETURNED
        first = store.get(case.case_token, 1)
        assert first is not None
        stored = [json.loads(payload) for payload in store.tracking_payloads(case.case_token)]
        store.resolve_returned_mail(
            case.case_token,
            saved.revision,
            ResponsibilityDecision(
                Responsibility.AGENCY_SIDE_EXISTING_REMEDY, "SYNTHETIC", ReturnCause.AGENCY_SIDE
            ),
            None,
            None,
            now=NOW + timedelta(hours=25),
        )
    app = app_for(path, payments, NOW + timedelta(hours=25))
    try:
        facts, cost, estimate = app._returned_mail_evidence(
            "probe", saved, first.request.recipient, ORIGINAL
        )
        proof = app._return_proof(saved, 1)
    finally:
        app._store.close()
    genuine_rows = [row for row in stored if row.get("usps_reason") == "REFUSED"]
    assert genuine_rows, "the probe's genuine return evidence was not stored"
    assert facts.return_cause is ReturnCause.AGENCY_SIDE
    assert cost == genuine_rows[0]["mailing_cost_cents"]
    assert estimate is True
    assert proof is not None
    assert [event.event for event in proof.events] == ["letter.returned_to_sender"]
    assert [event.occurred_at for event in proof.events] == [times[0]]


# --- LOW-3: "no charge" copy tracks whether a capture can still land ----------------


def test_no_charge_copy_distinguishes_a_pending_capture_from_a_terminal_stop(
    tmp_path: Path,
) -> None:
    """Only terminal no-charge states say "No charge was made." """
    path = tmp_path / "cases.sqlite"
    payments = FakePaymentClient()
    case, _late = returned_with_remail_intent(path, remail_lob(), expired=True)
    with SqliteTransactionStore(path) as store:
        saved = current_case(store, case)
    app = service(path, payments)
    try:
        for state in (
            PaymentState.AUTHORIZED,
            PaymentState.CAPTURE_PENDING,
            PaymentState.CAPTURE_RETRYING,
        ):
            remedy = app._return_remedy_status(replace(saved, payment_state=state), "probe")
            assert remedy is not None
            assert "No charge has been made yet." in remedy.message
            assert "No charge was made." not in remedy.message
        for state in (PaymentState.RELEASED, PaymentState.WRITTEN_OFF):
            remedy = app._return_remedy_status(replace(saved, payment_state=state), "probe")
            assert remedy is not None
            assert "No charge was made." in remedy.message
    finally:
        app._store.close()


# --- LOW-4: the retained scans are future work, not a shown delivery report ---------


def test_docs_keep_retained_scans_for_a_future_delivery_report() -> None:
    contract = (Path(__file__).parents[2] / "legacy/backend/docs/api-contract.md").read_text()
    architecture = (Path(__file__).parents[2] / "legacy/docs/architecture-v8.md").read_text()
    for name, text in (("api-contract.md", contract), ("architecture-v8.md", architecture)):
        claims = [block for block in text.split("\n\n") if "retained scans" in block]
        assert claims, name
        for block in claims:
            assert "not yet shown" in block, name
