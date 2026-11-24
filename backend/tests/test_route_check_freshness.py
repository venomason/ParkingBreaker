from datetime import timedelta
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker.api.app import create_app
from parkingbreaker.route_checks import MAX_ROUTE_CHECK_AGE
from parkingbreaker.route_registry import RouteScope
from parkingbreaker.transaction_store import SqliteTransactionStore
from samples import SAN_FRANCISCO
from test_case_service import ALICE, INTAKE, NOW, Clock, fake_resolution, service, submit

SCOPE = RouteScope(SAN_FRANCISCO.id, "first-level review", "certified")


def enroll(store: SqliteTransactionStore) -> None:
    store.change_route(
        scope=SCOPE,
        expected_revision=0,
        command_id="enroll-test",
        status="verified",
        address_fingerprint="a" * 64,
        source_url="https://sfmta.com/contest",
        source_quote="Send the first written protest to the Citation Review office.",
        evidence_sha256="b" * 64,
        source_text_sha256="c" * 64,
        valid_until=NOW + timedelta(days=1),
        now=NOW,
    )


def test_monitor_outage_late_result_and_hard_expiry(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite"
    with SqliteTransactionStore(path) as store:
        enroll(store)
        original = store.current_route(SCOPE)
        assert original is not None
        assert store.route_check_current(original, NOW)
        missed = NOW + MAX_ROUTE_CHECK_AGE
        assert not store.route_check_current(original, missed)
        assert store.withdrawn_address("a" * 64, missed)
        assert store.confirm_route_check(SCOPE, original.revision, missed)
        assert store.confirm_route_check(SCOPE, original.revision, NOW)
    with SqliteTransactionStore(path) as recovered:
        assert recovered.route_check_current(original, missed)
        assert not recovered.confirm_route_check(SCOPE, original.revision, NOW + timedelta(days=1))
        recovered.change_route(
            scope=SCOPE,
            expected_revision=1,
            command_id="office-moved",
            status="revoked",
            address_fingerprint=None,
            source_url=None,
            source_quote=None,
            evidence_sha256=None,
            valid_until=None,
            now=missed,
        )
        assert not recovered.confirm_route_check(SCOPE, original.revision, missed)
        assert not recovered.route_check_current(original, missed)


def test_observed_move_survives_a_busy_dispatcher_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite"
    with SqliteTransactionStore(path) as dispatcher, SqliteTransactionStore(path) as watcher:
        enroll(dispatcher)
        route = dispatcher.current_route(SCOPE)
        assert route is not None
        with dispatcher.submission_ownership():
            watcher.invalidate_route_check(route, "official_contest_passage_changed", NOW)
            assert not dispatcher.route_check_current(route, NOW)
    with SqliteTransactionStore(path) as recovered:
        assert recovered.route_invalidation(route) == "official_contest_passage_changed"
        assert not recovered.confirm_route_check(SCOPE, 1, NOW)
        assert recovered.withdrawn_address("a" * 64, NOW)


@pytest.mark.anyio
@pytest.mark.usefixtures("fake_resolution")
async def test_prehold_rechecks_monitored_route_before_new_payment(tmp_path: Path) -> None:
    clock = Clock()
    real = service(tmp_path / "test.sqlite", clock=clock)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(real)), base_url="http://test"
        ) as client:
            _, receipt = await submit(client, INTAKE)
            path = f"/cases/{receipt['case_id']}/pre-hold"
            enroll(real._store)
            assert (await client.get(path, headers=ALICE)).json()["kind"] == "offer"
            clock.now += MAX_ROUTE_CHECK_AGE
            stopped = (await client.get(path, headers=ALICE)).json()
            assert stopped["reason_key"] == "appeal_office_needs_verification"
            assert stopped["no_charge"]
            assert real._store.confirm_route_check(SCOPE, 1, clock.now)
            assert (await client.get(path, headers=ALICE)).json()["kind"] == "offer"
    finally:
        real._store.close()


assert fake_resolution
