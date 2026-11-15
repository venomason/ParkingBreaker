"""A beta entitlement starts the existing case machine without a financial instrument."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from typing import cast
from uuid import uuid4

import pytest

from mail_samples import make_request
from parkingbreaker import case_state as cs
from parkingbreaker.beta_store import BETA_DISCLOSURE_SHA256, BetaConflict
from parkingbreaker.case_status import CaseStatus
from parkingbreaker.mail_attempt_store import PreparationConflict
from parkingbreaker.payment_coordinator import run_payment_step
from parkingbreaker.payment_records import PaymentClient
from parkingbreaker.payment_state import CaptureRequested, PaymentState, allowed_payment_events
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_beta_admission import NOW, campaign, candidate


def selected(store: SqliteTransactionStore) -> None:
    campaign(store)
    candidate(store, 1)
    store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)


def test_consent_case_and_entitlement_commit_once_with_zero_customer_money(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        selected(store)
        with pytest.raises(BetaConflict, match="disclosure_changed"):
            store.start_beta_case("case_1", "0" * 64, NOW)
        with pytest.raises(BetaConflict, match="qualification_expired"):
            store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, NOW + timedelta(days=2))
        assert store._require_open().execute("SELECT COUNT(*) FROM pb_cases").fetchone() == (0,)
        case = store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, NOW)
        assert (case.amount_cents, case.payment_state, case.attempt) == (
            0,
            PaymentState.NO_CHARGE,
            1,
        )
        assert case.authorization_ref.startswith("beta_")
        assert store.payment_cursor(case.case_token) is None
        assert allowed_payment_events(PaymentState.NO_CHARGE) == frozenset()
        entitlement = store.beta_entitlement("case_1")
        assert entitlement is not None
        assert entitlement.case_token == case.case_token
        assert entitlement.accepted_at == NOW
        assert store._require_open().execute(
            "SELECT COUNT(*) FROM pb_intake_authorization_attempts"
        ).fetchone() == (0,)
    with SqliteTransactionStore(path) as store:
        assert (
            store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, NOW + timedelta(days=2)) == case
        )
        assert store.get_case(case.case_token) == case
        assert store._require_open().execute("SELECT COUNT(*) FROM pb_cases").fetchone() == (1,)


def test_concurrent_beta_acceptance_cannot_start_two_cases(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        selected(store)
    ready = Barrier(2)

    def accept(_: int) -> str:
        with SqliteTransactionStore(path) as store:
            ready.wait(timeout=10)
            return store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, NOW).case_token

    with ThreadPoolExecutor(max_workers=2) as workers:
        tokens = list(workers.map(accept, (1, 2)))
    assert len(set(tokens)) == 1
    with SqliteTransactionStore(path) as store:
        assert store._require_open().execute("SELECT COUNT(*) FROM pb_cases").fetchone() == (1,)


def test_beta_stops_and_mailing_truth_never_emit_money_or_a_second_submission(
    tmp_path: Path,
) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        selected(store)
        case = store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, NOW)
        for event in (
            cs.ResearchComplete(),
            cs.DraftReady(),
            cs.CustomerApproved(),
            cs.SubmitRequested(),
            cs.SubmitConfirmed("synthetic-letter"),
            cs.ReturnConfirmed("synthetic-return"),
        ):
            case = store.apply_letter_event(
                case.case_token,
                event,
                case.revision,
                occurred_at=NOW,
                new_effect_id=lambda: uuid4().hex,
            )
        assert case.payment_state is PaymentState.NO_CHARGE
        assert all(
            row.machine != "payment"
            and row.effect
            not in {
                cs.Effect.CAPTURE_PAYMENT,
                cs.Effect.RELEASE_HOLD,
            }
            for row in store.effects(case.case_token)
        )
        assert store.payment_cursor(case.case_token) is None
        before = store.transition_history(case.case_token)
        with pytest.raises(ValueError, match="second-submission"):
            store.apply_letter_event(
                case.case_token, cs.RemailRequested(), case.revision, occurred_at=NOW
            )
        request = make_request(token="beta-one-approved-submission")
        first = store.prepare(case.case_token, 1, request, NOW)
        assert store.prepare(case.case_token, 1, request, NOW) == first
        with pytest.raises(PreparationConflict, match="one distinct submission"):
            store.prepare(case.case_token, 2, request, NOW)
        assert store.transition_history(case.case_token) == before


def test_no_charge_stop_is_settled_and_all_payment_entry_points_refuse(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        selected(store)
        case = store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, NOW)
        with pytest.raises(ValueError, match="payment"):
            store.apply_payment_event(
                case.case_token, CaptureRequested(), case.revision, occurred_at=NOW
            )
        with pytest.raises(ValueError, match="no-charge case cannot dispatch payment"):
            run_payment_step(
                store,
                cast(PaymentClient, object()),
                case.case_token,
                expected_case_revision=case.revision,
                expected_cursor_revision=None,
                operation_id="operation-1",
                key="payment-key",
                clock=lambda: NOW,
            )
        with (
            store.submission_ownership(),
            pytest.raises(ValueError, match="no-charge case cannot dispatch payment"),
        ):
            store.claim_payment(
                case.case_token,
                case.revision,
                None,
                now=NOW,
                operation_id="operation-1",
                key="payment-key",
                retry_effect_id="retry-1",
            )
        case = store.apply_letter_event(
            case.case_token,
            cs.Stopped(cs.StopReason.NOT_YET_VERIFIED, hard_stop=cs.HardStop.S2),
            case.revision,
            occurred_at=NOW,
            new_effect_id=lambda: uuid4().hex,
        )
        assert case.case_status is CaseStatus.DONE
        assert store.payment_cursor(case.case_token) is None
        assert store._require_open().execute(
            "SELECT COUNT(*) FROM pb_payment_operations"
        ).fetchone() == (0,)


def test_v4_paid_database_migrates_without_changing_case_or_child_records(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        store.save_workspace("paid-workspace", Workspace(notes="preserve"), NOW)
        paid = store.start_case(
            "paid-workspace", "paid-authorization", NOW, NOW + timedelta(days=5)
        )
        paid = store.apply_letter_event(
            paid.case_token,
            cs.ResearchComplete(),
            paid.revision,
            occurred_at=NOW,
        )
        before = store.transition_history(paid.case_token)
    db = sqlite3.connect(path, isolation_level=None)
    db.executescript("""PRAGMA foreign_keys=OFF; PRAGMA legacy_alter_table=ON;
        BEGIN IMMEDIATE;
        ALTER TABLE pb_cases RENAME TO pb_cases_v5;
        CREATE TABLE pb_cases (
            case_token TEXT PRIMARY KEY,
            workspace_token TEXT NOT NULL REFERENCES pb_workspaces(workspace_token),
            authorization_ref TEXT NOT NULL UNIQUE, authorized_at TEXT NOT NULL,
            authorization_expires_at TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
            letter_state TEXT NOT NULL, payment_state TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
            revision INTEGER NOT NULL CHECK (revision >= 1)
        );
        INSERT INTO pb_cases SELECT * FROM pb_cases_v5;
        DROP TABLE pb_cases_v5;
        UPDATE pb_store_versions SET version=4 WHERE component='transactions';
        COMMIT;""")
    db.close()
    with SqliteTransactionStore(path) as store:
        assert store.get_case(paid.case_token) == paid
        assert store.transition_history(paid.case_token) == before
        assert store._require_open().execute("PRAGMA foreign_key_check").fetchall() == []
        assert store._require_open().execute(
            "SELECT version FROM pb_store_versions WHERE component='transactions'"
        ).fetchone() == (5,)
        with pytest.raises(ValueError, match="allowed service price"):
            store.start_case("paid-workspace", "not-free", NOW, NOW + timedelta(days=5), 0)
