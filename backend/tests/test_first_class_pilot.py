"""The First-Class pilot stays within its stored issuer, window and quota."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from parkingbreaker import transaction_store
from parkingbreaker.agencies import IssuerType, ResolvedAgency
from parkingbreaker.beta_store import (
    _V1_DDL,
    BETA_DISCLOSURE_SHA256,
    FIRST_CLASS_PILOT_DISCLOSURE_SHA256,
    BetaConflict,
    BetaEntitlement,
    FirstClassPilot,
)
from parkingbreaker.mail_config import MailClass
from parkingbreaker.payment_records import mutation_key
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.research_records import agency_json
from parkingbreaker.route_registry import RouteRevision, RouteScope
from parkingbreaker.service_phase import ServicePhase
from parkingbreaker.session_window import DEFAULT_WINDOW
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_beta_acceptance_api import INTAKE
from test_beta_admission import END, NOW, START

CAMPAIGN = "first-class-pilot"
SCOPE = RouteScope("CA:issuer-1.gov", "initial review", MailClass.FIRST_CLASS)
DIGEST = "a" * 64


def enroll(
    store: SqliteTransactionStore, *, scope: RouteScope = SCOPE, now: datetime = NOW
) -> RouteRevision:
    return store.change_route(
        scope=scope,
        expected_revision=0,
        command_id=f"enroll-{scope.mail_class}",
        status="verified",
        address_fingerprint="b" * 64,
        source_url="https://issuer-1.gov/appeal",
        source_quote="Mail initial review requests to Fixture Parking Review.",
        evidence_sha256=DIGEST,
        valid_until=now + timedelta(days=20),
        now=now,
    )


def open_pilot(store: SqliteTransactionStore, now: datetime = NOW) -> FirstClassPilot:
    return store.open_first_class_pilot(
        CAMPAIGN, SCOPE, "identity-v1", "owner-approval", DIGEST, now
    )


def save_intake(
    store: SqliteTransactionStore,
    number: int,
    *,
    scope: RouteScope = SCOPE,
    owner: str | None = None,
    citation: str | None = None,
    now: datetime = NOW,
) -> str:
    case = f"case_{number}"
    workspace = f"ws_{number}"
    store.create_owned_workspace(
        owner or f"owner-{number}", workspace, Workspace(), now, create_new=True
    )
    payload = deepcopy(INTAKE)
    payload["ticket"].update(
        citation_number=citation or f"PILOT-{number}",
        issue_date=now.date().isoformat(),
        stage=scope.stage,
    )
    saved_offer = {
        "kind": "offer",
        "issuer": {"issuer_id": scope.issuer, "stage": scope.stage},
        "tiers": [{"tier": "CERTIFIED"}],
    }
    agency = ResolvedAgency(
        scope.issuer,
        "Fixture Issuer",
        "CA",
        (scope.issuer.removeprefix("CA:"),),
        IssuerType.MUNICIPAL,
        "Fixture Parking Review",
        "CA",
    )
    store.record_intake(
        case, workspace, json.dumps(payload), json.dumps(saved_offer), now, agency_json(agency)
    )
    return case


def test_pilot_scope_and_window_are_fixed_and_ordinary_route_must_be_enrolled(
    tmp_path: Path,
) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        certified = RouteScope(SCOPE.issuer, SCOPE.stage, MailClass.CERTIFIED)
        enroll(store, scope=certified)
        with pytest.raises(BetaConflict, match="first_class_pilot_route_unavailable"):
            open_pilot(store)
        assert store.first_class_pilot_status() is None

        route = enroll(store)
        with pytest.raises(BetaConflict, match="first_class_pilot_route_evidence_changed"):
            store.open_first_class_pilot(CAMPAIGN, SCOPE, "identity-v1", "approval", "b" * 64, NOW)
        pilot = open_pilot(store)
        assert (pilot.starts_at, pilot.ends_at) == (NOW, NOW + timedelta(days=14))
        assert open_pilot(store, NOW + timedelta(days=2)) == pilot
        with pytest.raises(BetaConflict, match="first_class_pilot_identity_changed"):
            store.open_first_class_pilot("another", SCOPE, "identity-v1", "approval", DIGEST, NOW)

        wrong_issuer = RouteScope("CA:issuer-2.gov", SCOPE.stage, MailClass.FIRST_CLASS)
        wrong_stage = RouteScope(SCOPE.issuer, "hearing request", MailClass.FIRST_CLASS)
        assert store.offer_first_class_pilot(save_intake(store, 1, scope=wrong_issuer), NOW) is None
        assert store.offer_first_class_pilot(save_intake(store, 2, scope=wrong_stage), NOW) is None
        case = save_intake(store, 3)
        assert store.offer_first_class_pilot(case, NOW + timedelta(hours=1)) is None
        assert store.confirm_route_check(SCOPE, route.revision, pilot.ends_at)
        fresh_case = save_intake(store, 4, now=pilot.ends_at)
        assert store.offer_first_class_pilot(fresh_case, pilot.ends_at) is None
        assert store.first_class_pilot_status() == pilot


def test_slots_persist_and_concurrent_selections_cannot_exceed_five(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        enroll(store)
        pilot = open_pilot(store)
        cases = [save_intake(store, number) for number in range(1, 8)]
        first = tuple(store.offer_first_class_pilot(case, NOW) for case in cases[:2])
        assert all(item is not None and item.pilot_scope == SCOPE for item in first)

    ready = Barrier(5)

    def select(case: str) -> BetaEntitlement | None:
        with SqliteTransactionStore(path) as store:
            ready.wait(timeout=10)
            return store.offer_first_class_pilot(case, NOW)

    with ThreadPoolExecutor(max_workers=5) as executor:
        later = tuple(executor.map(select, cases[2:]))
    assert sum(item is not None for item in later) == 3

    with SqliteTransactionStore(path) as store:
        resumed = store.first_class_pilot_status()
        assert resumed is not None
        assert resumed.selected_count == 5
        assert resumed.ends_at == pilot.ends_at
        assert tuple(store.offer_first_class_pilot(case, NOW) for case in cases[:2]) == first
        db = store._require_open()
        assert db.execute("SELECT COUNT(*) FROM pb_beta_candidates").fetchone() == (5,)
        assert db.execute("SELECT COUNT(*) FROM pb_beta_batches").fetchone() == (5,)
        assert db.execute("SELECT COUNT(*) FROM pb_intake_authorization_attempts").fetchone() == (
            0,
        )
        sixth = next(case for case in cases if store.beta_entitlement(case) is None)
        db.execute(
            "INSERT INTO pb_beta_batches VALUES (?, ?, ?, ?, NULL, NULL)",
            ("extra-batch", CAMPAIGN, "owner-extra", NOW.isoformat()),
        )
        with pytest.raises(sqlite3.IntegrityError, match="first-class pilot is full"):
            db.execute(
                "INSERT INTO pb_beta_entitlements VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                (
                    "extra",
                    sixth,
                    "extra-batch",
                    CAMPAIGN,
                    SCOPE.issuer,
                    DIGEST,
                    DIGEST,
                    pilot.ends_at.isoformat(),
                    NOW.isoformat(),
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="pilot selections are final"):
            db.execute("DELETE FROM pb_beta_entitlements WHERE intake_id=?", (cases[0],))


def test_duplicate_owner_or_citation_does_not_consume_another_slot(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        enroll(store)
        open_pilot(store)
        first = save_intake(store, 1, owner="returning-driver", citation="AB-123")
        assert store.offer_first_class_pilot(first, NOW) is not None
    with SqliteTransactionStore(path) as store:
        same_owner = save_intake(store, 2, owner="returning-driver")
        same_ticket = save_intake(store, 3, citation=" ab\u2013123 ")
        assert store.offer_first_class_pilot(same_owner, NOW) is None
        assert store.offer_first_class_pilot(same_ticket, NOW) is None
        other = save_intake(store, 4, citation="AB-124")
        assert store.offer_first_class_pilot(other, NOW) is not None
        pilot = store.first_class_pilot_status()
        assert pilot is not None
        assert pilot.selected_count == 2


def test_phase_pausing_and_any_previous_paid_intent_refuse_selection(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        enroll(store)
        open_pilot(store)
        saved = save_intake(store, 1)
        case = store.get_intake(saved)
        assert case is not None
        store.update_intake(saved, case.revision, selected_tier="CERTIFIED", case_token=None)
        request = {
            "amount_cents": 2999,
            "currency": "usd",
            "metadata": {"case": saved, "tier": "CERTIFIED", "service_amount_cents": "2999"},
            "payment_method_id": "pm_fixture",
            "tier": "CERTIFIED",
        }
        key = mutation_key("authorization", saved, "CERTIFIED:2999", "pm_fixture")
        store.begin_authorization_attempt(
            saved,
            key,
            "CERTIFIED",
            2999,
            "pm_fixture",
            json.dumps(request, sort_keys=True, separators=(",", ":")),
            NOW,
        )
        store.record_authorization_outcome(saved, key, "declined", None, NOW)
        assert store.offer_first_class_pilot(saved, NOW) is None

        held = save_intake(store, 2)
        store.set_runtime_service_phase(ServicePhase.PAID, 0, "paid", "Ops", "Test", now=NOW)
        assert store.offer_first_class_pilot(held, NOW) is None
        store.set_runtime_service_phase(
            ServicePhase.CONTROLLED_FREE_BETA, 1, "free", "Ops", "Test", now=NOW
        )
        store.set_intake_pause(True, 0, "stop", "Ops", "Test", now=NOW)
        assert store.offer_first_class_pilot(held, NOW) is None
        store.set_intake_pause(False, 1, "resume", "Ops", "Test", now=NOW)
        assert store.offer_first_class_pilot(held, NOW) is not None


def test_pilot_consent_checks_its_own_disclosure_and_current_route(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        route = enroll(store)
        open_pilot(store)
        case_id = save_intake(store, 1)
        entitlement = store.offer_first_class_pilot(case_id, NOW)
        assert entitlement is not None
        assert entitlement.tier == "STANDARD"
        assert entitlement.disclosure_sha256 == FIRST_CLASS_PILOT_DISCLOSURE_SHA256
        assert "ordinary USPS First-Class Mail" in entitlement.disclosure_text
        assert "skip follow-up" in entitlement.disclosure_text

        db = store._require_open()
        db.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(BetaConflict, match="beta_disclosure_changed"):
                store._beta_acceptance(case_id, BETA_DISCLOSURE_SHA256, NOW)
            assert (
                store._beta_acceptance(case_id, entitlement.disclosure_sha256, NOW) == entitlement
            )
            with pytest.raises(BetaConflict, match="beta_qualification_expired"):
                store._beta_acceptance(
                    case_id, entitlement.disclosure_sha256, entitlement.eligible_until
                )
        finally:
            db.rollback()
        store.invalidate_route_check(route, "office_changed", NOW)
        db.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(BetaConflict, match="first_class_pilot_route_unavailable"):
                store._beta_acceptance(case_id, entitlement.disclosure_sha256, NOW)
        finally:
            db.rollback()


def test_v1_accepted_certified_case_survives_schema_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.sqlite"

    def initialize_v1(db: sqlite3.Connection) -> None:
        for ddl in _V1_DDL.values():
            db.execute(ddl)
        db.execute("INSERT INTO pb_store_versions VALUES ('free_beta', 1)")
        db.execute(
            "CREATE TRIGGER pb_beta_batch_five BEFORE INSERT ON pb_beta_entitlements "
            "WHEN (SELECT COUNT(*) FROM pb_beta_entitlements WHERE batch_id=NEW.batch_id) >= 5 "
            "BEGIN SELECT RAISE(ABORT, 'beta batch is full'); END"
        )

    with monkeypatch.context() as patch:
        patch.setattr(transaction_store, "initialize_beta", initialize_v1)
        with SqliteTransactionStore(path) as store:
            intake = save_intake(store, 1)
            db = store._require_open()
            stamp = NOW.isoformat()
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO pb_beta_campaigns VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "legacy",
                    START.isoformat(),
                    END.isoformat(),
                    "identity-v1",
                    "owner-approval",
                    stamp,
                ),
            )
            db.execute(
                "INSERT INTO pb_beta_batches VALUES (?, ?, ?, ?, NULL, NULL)",
                ("legacy-batch", "legacy", "legacy-approved", stamp),
            )
            case = store._start_case_in_transaction(
                "ws_1", "beta_legacy", NOW, NOW + DEFAULT_WINDOW, 0, no_charge=True
            )
            db.execute(
                "UPDATE pb_intake_records SET case_token=?, selected_tier='CERTIFIED' "
                "WHERE intake_id=?",
                (case.case_token, intake),
            )
            db.execute(
                "INSERT INTO pb_beta_entitlements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy",
                    intake,
                    "legacy-batch",
                    "legacy",
                    SCOPE.issuer,
                    DIGEST,
                    DIGEST,
                    END.isoformat(),
                    stamp,
                    stamp,
                    BETA_DISCLOSURE_SHA256,
                    case.case_token,
                ),
            )
            db.commit()

    with SqliteTransactionStore(path) as store:
        assert store.first_class_pilot_status() is None
        entitlement = store.beta_entitlement(intake)
        assert entitlement is not None
        assert entitlement.pilot_scope is None
        assert (entitlement.tier, entitlement.disclosure_version) == ("CERTIFIED", 1)
        assert entitlement.disclosure_sha256 == BETA_DISCLOSURE_SHA256
        replay = store.start_beta_case(intake, BETA_DISCLOSURE_SHA256, END + timedelta(days=1))
        assert replay.case_token == case.case_token
        assert replay.payment_state is PaymentState.NO_CHARGE
        assert store._require_open().execute("PRAGMA foreign_key_check").fetchall() == []
    with SqliteTransactionStore(path) as store:
        assert store.beta_entitlement(intake) == entitlement


def test_private_cli_opens_and_selects_from_saved_intake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cases.sqlite"
    command = [sys.executable, "-m", "parkingbreaker.beta_campaign_cli", "--database", str(path)]

    def run(*args: str, expect: int = 0) -> object:
        result = subprocess.run([*command, *args], capture_output=True, check=False, text=True)
        assert result.returncode == expect, result.stderr
        return json.loads(result.stdout if expect == 0 else result.stderr)

    assert run("status", expect=1) == {"error": "existing_beta_database_required"}
    assert not path.exists()
    now = datetime.now(UTC)
    with SqliteTransactionStore(path) as store:
        enroll(store, now=now)
        case_id = save_intake(store, 1, now=now)
    assert run("status") == {"pilot": None}
    assert (
        run(
            "open-first-class",
            "--campaign-id",
            CAMPAIGN,
            "--issuer",
            SCOPE.issuer,
            "--stage",
            SCOPE.stage,
            "--identity-policy-ref",
            "identity-v1",
            "--configuration-ref",
            "owner-approval",
            "--evidence-sha256",
            DIGEST,
        )
        is not None
    )
    select = ("select", "--campaign-id", CAMPAIGN, "--case-id", case_id)
    monkeypatch.setenv("PB_SERVICE_PHASE", ServicePhase.PAID)
    assert run(*select, expect=1) == {"error": "first_class_pilot_offer_unavailable"}
    monkeypatch.setenv("PB_SERVICE_PHASE", ServicePhase.CONTROLLED_FREE_BETA)
    selected = run(*select)
    assert isinstance(selected, dict)
    assert selected["selected_case_id"] == case_id
    assert run(*select) == selected
    (tmp_path / "RESTORE_QUARANTINE.json").touch()
    assert run(*select, expect=1) == {"error": "beta_management_disabled_on_restored_data"}
