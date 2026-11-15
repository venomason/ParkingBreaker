"""Selected free entitlements cannot enter the real paid authorization seams."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from typing import cast

import pytest

from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.beta_store import BETA_DISCLOSURE_SHA256, BetaConflict
from parkingbreaker.case_service import CaseService
from parkingbreaker.config import ProviderMode, Settings, build_services
from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.payment_records import PaymentClient, mutation_key
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_beta_admission import NOW, campaign, candidate


def reserve(store: SqliteTransactionStore) -> str:
    iid = "case_1"
    pm = "pm_fixture"
    key = mutation_key("authorization", iid, "CERTIFIED:2999", pm)
    request = {
        "amount_cents": 2999,
        "currency": "usd",
        "metadata": {"case": iid, "tier": "CERTIFIED", "service_amount_cents": "2999"},
        "payment_method_id": pm,
        "tier": "CERTIFIED",
    }
    store.begin_authorization_attempt(
        iid,
        key,
        "CERTIFIED",
        2999,
        pm,
        json.dumps(request, sort_keys=True, separators=(",", ":")),
        NOW,
    )
    return key


def prepare(store: SqliteTransactionStore) -> None:
    campaign(store)
    candidate(store, 1)
    row = store.get_intake("case_1")
    assert row is not None
    store.update_intake("case_1", row.revision, selected_tier="CERTIFIED", case_token=None)


class Forbidden:
    def __getattribute__(self, name: str) -> object:
        raise AssertionError("payment dependency touched: " + name)


@pytest.mark.parametrize("accepted", [False, True])
def test_service_and_store_refuse_selected_beta_before_payment_access(
    tmp_path: Path, accepted: bool
) -> None:
    service = build_services(Settings(ProviderMode.FAKE, tmp_path, "worker-secret-fixture", {}))
    assert isinstance(service, CaseService)
    store = service._store
    try:
        prepare(store)
        store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)
        if accepted:
            store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, NOW)
        before = tuple(store._require_open().iterdump())
        service._deps = replace(service._deps, payments=cast(PaymentClient, Forbidden()))
        with pytest.raises(ServiceConflict, match="beta_no_payment_required"):
            service.authorize(
                "case_1", s.StartAuthorization(terms_accepted=True, payment_method_id="pm_fixture")
            )
        if accepted:
            ent = store.beta_entitlement("case_1")
            assert ent is not None
            assert ent.case_token is not None
            with pytest.raises(ServiceConflict, match="beta_no_payment_required"):
                service._authorization("case_1", ent.case_token)
        with pytest.raises(IntakeConflict, match="beta_no_payment_required"):
            reserve(store)
        assert tuple(store._require_open().iterdump()) == before
    finally:
        store.close()


@pytest.mark.parametrize("state", ["pending", "authorized"])
def test_dispatch_refuses_historical_attempt_before_replay(tmp_path: Path, state: str) -> None:
    with SqliteTransactionStore(tmp_path / "db") as store:
        prepare(store)
        key = reserve(store)
        store.record_authorization_outcome("case_1", key, "declined", None, NOW)
        store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)
        db = store._require_open()
        db.execute(
            "UPDATE pb_intake_authorization_attempts SET state=?, authorization_ref=?, "
            "authorized_at=?, authorization_expires_at=? WHERE intake_id=?",
            (
                state,
                None if state == "pending" else "pi_fixture",
                None if state == "pending" else NOW.isoformat(),
                None if state == "pending" else (NOW + timedelta(days=5)).isoformat(),
                "case_1",
            ),
        )
        before = tuple(db.iterdump())
        with pytest.raises(IntakeConflict, match="beta_no_payment_required"):
            store.claim_authorization_dispatch("case_1", key, NOW, timedelta(hours=1))
        assert tuple(db.iterdump()) == before


def test_selection_racing_paid_reservation_never_grants_both(tmp_path: Path) -> None:
    path = tmp_path / "db"
    with SqliteTransactionStore(path) as store:
        prepare(store)
    barrier = Barrier(2)

    def run(select: bool) -> bool:
        with SqliteTransactionStore(path) as store:
            barrier.wait(timeout=10)
            try:
                if select:
                    store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)
                else:
                    reserve(store)
                return True
            except (IntakeConflict, BetaConflict):
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [True, False]))
    assert sum(results) == 1
    with SqliteTransactionStore(path) as store:
        selected = store.beta_entitlement("case_1") is not None
        pending = store.has_active_authorization_attempt("case_1")
        assert selected != pending
