"""Selected-customer free-beta offer and acceptance through the real case API."""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest
from httpx2 import ASGITransport, AsyncClient, Response

from parkingbreaker import case_state as cs
from parkingbreaker.agencies import IssuerType, ResolvedAgency
from parkingbreaker.api import schemas as s
from parkingbreaker.api.app import create_app
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.beta_store import (
    BETA_DISCLOSURE_SHA256,
    BETA_DISCLOSURE_TEXT,
    BETA_DISCLOSURE_VERSION,
    BetaConflict,
)
from parkingbreaker.blob_store import FakeBlobStore
from parkingbreaker.case_service import CaseService, CaseServiceDeps
from parkingbreaker.payment_records import PaymentClient
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.prehold import PreholdDeps
from parkingbreaker.research_records import agency_json
from parkingbreaker.service_policy import SERVICE_CONTROL, STATE_BASIS
from parkingbreaker.session_window import DEFAULT_WINDOW
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_beta_admission import END, NOW, START

pytestmark = pytest.mark.anyio
ALICE = {"Authorization": "Bearer fake-alice"}
BOB = {"Authorization": "Bearer fake-bob"}
WINDOW = DEFAULT_WINDOW

INTAKE: dict[str, Any] = {
    "rental_company_paid": "NOT_RENTAL",
    "ticket": {
        "citation_number": "BETA-0001",
        "license_plate": "BETA123",
        "license_plate_state": "CA",
        "issuer_hint": "Fixture Municipal Parking Agency",
        "issued_city": "Fixture City",
        "issued_state": "CA",
        "violation_code": "BETA-7",
        "violation_description": "Meter expired",
        "issue_date": "2026-11-01",
        "stage": "first-level review",
    },
    "customer_position": {"statement": "I paid for parking and have the receipt."},
    "contact": {
        "name": "Alex Example",
        "phone": "415-555-0100",
        "email": "alex@example.test",
        "mailing_address": {
            "line1": "123 Example Street",
            "city": "Fixture City",
            "state": "CA",
            "zip_code": "90000",
        },
    },
    "photo_refs": [],
}


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ForbiddenPayments:
    """Fail on any payment dependency access; beta acceptance must never reach it."""

    authorization_evidence_required = True

    def __getattr__(self, name: str) -> object:
        raise AssertionError("payment dependency touched: " + name)


def offer_json(number: int) -> str:
    return json.dumps(
        {
            "kind": "offer",
            "case_id": f"case_{number}",
            "issuer": {
                "issuer_id": f"CA:issuer-{number}.gov",
                "issuer_name": f"Fixture issuer {number}",
                "deciding_body": "Fixture Parking Review",
                "state": "CA",
                "stage": "first-level review",
                "service_basis": "FAKE_BETA_FIXTURE",
                "delegated_tenant": None,
                "evidence_ref": "fixture://beta",
            },
            "tiers": [
                {
                    "tier": "CERTIFIED",
                    "amount_cents": 2999,
                    "currency": "usd",
                    "description": "Certified mail with tracking; no promised arrival date.",
                }
            ],
        }
    )


def create_candidate(
    store: SqliteTransactionStore,
    number: int,
    *,
    owner: str = "alice",
    jurisdiction: int | None = None,
    eligible_until: datetime | None = None,
    intake: dict[str, Any] | None = None,
) -> str:
    """Trusted qualification fixture: the customer cannot create this entitlement."""
    token = f"case_{number}"
    issuer = f"CA:issuer-{number}.gov"
    agency = ResolvedAgency(
        issuer,
        f"Fixture issuer {number}",
        "CA",
        (f"issuer-{number}.gov",),
        IssuerType.MUNICIPAL,
        "Parking review",
        "CA",
    )
    store.create_owned_workspace(owner, f"ws_{number}", Workspace(), NOW)
    store.record_intake(
        token,
        f"ws_{number}",
        json.dumps(intake if intake is not None else INTAKE),
        offer_json(number),
        NOW,
        agency_json=agency_json(agency),
    )
    digest = f"{number:064x}"
    store.bind_beta_jurisdiction(
        "beta-1",
        issuer,
        f"jurisdiction-{number if jurisdiction is None else jurisdiction}",
        digest,
        NOW,
    )
    store.qualify_beta_candidate(
        "beta-1",
        token,
        digest,
        eligible_until if eligible_until is not None else NOW + timedelta(days=1),
        NOW,
    )
    return token


def selected(path: Path, **kwargs: Any) -> None:
    with SqliteTransactionStore(path) as store:
        store.create_beta_campaign(
            "beta-1", START, END, "identity-fixture-v1", "owner-fixture", NOW
        )
        create_candidate(store, 1, **kwargs)
        store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)


def build_service(
    path: Path,
    *,
    clock: Callable[[], datetime] = lambda: NOW,
    payments: PaymentClient | None = None,
) -> CaseService:
    unused: Any = object()
    counter = iter(range(1000))
    deps = PreholdDeps(
        unused, None, unused, unused, unused, lambda: NOW, 25, STATE_BASIS, SERVICE_CONTROL
    )
    return CaseService(
        CaseServiceDeps(
            SqliteTransactionStore(path),
            deps,
            clock,
            payments if payments is not None else cast(PaymentClient, ForbiddenPayments()),
            FakeBlobStore(),
            new_id=lambda: f"{next(counter):032x}",
        )
    )


def client(service: CaseService) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=create_app(service)), base_url="http://real.test"
    )


async def accept(api: AsyncClient, digest: str = BETA_DISCLOSURE_SHA256) -> Response:
    return await api.post(
        "/cases/case_1/beta-acceptance",
        headers=ALICE,
        json={"terms_accepted": True, "disclosure_sha256": digest},
    )


def counts(store: SqliteTransactionStore) -> dict[str, int]:
    db = store._require_open()
    tables = (
        "pb_cases",
        "pb_beta_entitlements",
        "pb_intake_authorization_attempts",
        "pb_payment_operations",
        "pb_pending_effects",
        "pb_mail_attempts",
        "pb_submission_operations",
    )
    result: dict[str, int] = {}
    for table in tables:
        row = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        assert row is not None
        result[table] = int(row[0])
    return result


async def test_selected_offer_acceptance_replay_and_reopen_are_zero_charge(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    selected(path)
    service = build_service(path)
    try:
        async with client(service) as api:
            offer = await api.get("/cases/case_1/beta-offer", headers=ALICE)
            assert offer.status_code == 200, offer.text
            assert offer.json() == {
                "case_id": "case_1",
                "funding": "NO_CHARGE",
                "amount_cents": 0,
                "currency": "usd",
                "tier": "CERTIFIED",
                "disclosure_version": BETA_DISCLOSURE_VERSION,
                "disclosure_text": BETA_DISCLOSURE_TEXT,
                "disclosure_sha256": BETA_DISCLOSURE_SHA256,
                "eligible_until": (NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                "accepted_at": None,
            }
            assert not {"payment_intent_id", "client_secret", "authorization_ref"} & set(
                offer.json()
            )
            first = await accept(api)
            assert first.status_code == 200, first.text
            receipt = first.json()
            assert receipt == {
                "case_id": "case_1",
                "funding": "NO_CHARGE",
                "amount_cents": 0,
                "currency": "usd",
                "tier": "CERTIFIED",
                "accepted_at": NOW.isoformat().replace("+00:00", "Z"),
                "window_started_at": NOW.isoformat().replace("+00:00", "Z"),
                "window_expires_at": (NOW + WINDOW).isoformat().replace("+00:00", "Z"),
            }
            assert not {"payment_intent_id", "client_secret"} & set(receipt)
            assert (await accept(api)).json() == receipt
            wrong = await accept(api, "0" * 64)
            assert wrong.status_code == 409
            assert wrong.json() == {"detail": "beta_disclosure_changed"}
            assert counts(service._store) == {
                "pb_cases": 1,
                "pb_beta_entitlements": 1,
                "pb_intake_authorization_attempts": 0,
                "pb_payment_operations": 0,
                "pb_pending_effects": 1,
                "pb_mail_attempts": 0,
                "pb_submission_operations": 0,
            }
            machines = {
                row[0]
                for row in service._store._require_open().execute(
                    "SELECT machine FROM pb_pending_effects"
                )
            }
            assert machines == {"counter"}
            entitlement = service._store.beta_entitlement("case_1")
            assert entitlement is not None
            assert entitlement.accepted_at == NOW
            case = service._store.get_case(entitlement.case_token or "")
            assert case is not None
            assert (case.letter_state, case.payment_state, case.amount_cents) == (
                cs.CaseState.RESEARCHING,
                PaymentState.NO_CHARGE,
                0,
            )
    finally:
        service._store.close()

    # A fresh process/store reconstructs the identical receipt and accepted offer.
    reopened = build_service(path, clock=Clock(NOW + timedelta(days=5)))
    try:
        async with client(reopened) as api:
            assert (await accept(api)).json() == receipt
            persisted = await api.get("/cases/case_1/beta-offer", headers=ALICE)
            assert persisted.status_code == 200
            assert persisted.json()["accepted_at"] == receipt["accepted_at"]
            assert persisted.json()["eligible_until"] == (
                NOW + timedelta(days=1)
            ).isoformat().replace("+00:00", "Z")
            assert counts(reopened._store)["pb_cases"] == 1
    finally:
        reopened._store.close()


async def test_ownership_unselected_and_unauthenticated_are_refused(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pb.sqlite"
    with SqliteTransactionStore(path) as store:
        store.create_beta_campaign(
            "beta-1", START, END, "identity-fixture-v1", "owner-fixture", NOW
        )
        create_candidate(store, 1)
        create_candidate(store, 2, owner="bob", jurisdiction=1)
        store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW, randbelow=lambda _: 0)
    service = build_service(path)
    try:
        async with client(service) as api:
            own = await api.get("/cases/case_2/beta-offer", headers=BOB)
            assert own.status_code == 409
            assert own.json() == {"detail": "beta_entitlement_required"}
            assert (await api.get("/cases/case_1/beta-offer", headers=BOB)).status_code == 404
            assert (await api.get("/cases/missing/beta-offer", headers=BOB)).status_code == 404
            assert (await api.get("/cases/case_2/beta-offer")).status_code == 401
            body = {"terms_accepted": True, "disclosure_sha256": BETA_DISCLOSURE_SHA256}
            assert (
                await api.post("/cases/case_2/beta-acceptance", headers=BOB, json=body)
            ).json() == {"detail": "beta_entitlement_required"}
            assert (
                await api.post("/cases/case_1/beta-acceptance", headers=BOB, json=body)
            ).status_code == 404
            assert (
                await api.post("/cases/missing/beta-acceptance", headers=BOB, json=body)
            ).status_code == 404
            assert (await api.post("/cases/case_2/beta-acceptance", json=body)).status_code == 401
            assert counts(service._store)["pb_cases"] == 0
    finally:
        service._store.close()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"terms_accepted": False, "disclosure_sha256": BETA_DISCLOSURE_SHA256},
        {"terms_accepted": True},
        {"terms_accepted": True, "disclosure_sha256": "not-a-digest"},
        {
            "terms_accepted": True,
            "disclosure_sha256": BETA_DISCLOSURE_SHA256,
            "payment_method_id": "pm_card_visa",
        },
        {"terms_accepted": True, "disclosure_sha256": BETA_DISCLOSURE_SHA256, "amount_cents": 0},
        {
            "terms_accepted": True,
            "disclosure_sha256": BETA_DISCLOSURE_SHA256,
            "campaign_id": "beta-1",
        },
        {
            "terms_accepted": True,
            "disclosure_sha256": BETA_DISCLOSURE_SHA256,
            "entitlement_id": "forged",
        },
    ],
)
async def test_bad_fields_are_sanitized_and_never_start_work(
    tmp_path: Path, body: dict[str, Any]
) -> None:
    path = tmp_path / "pb.sqlite"
    selected(path)
    service = build_service(path)
    try:
        async with client(service) as api:
            response = await api.post("/cases/case_1/beta-acceptance", headers=ALICE, json=body)
            assert response.status_code == 422
            assert response.json() == {"detail": "invalid_request"}
            assert "forged" not in response.text
            assert "pm_card" not in response.text
            assert counts(service._store)["pb_cases"] == 0
            entitlement = service._store.beta_entitlement("case_1")
            assert entitlement is not None
            assert entitlement.accepted_at is None
    finally:
        service._store.close()


async def test_stale_disclosure_is_refused_without_acceptance(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    selected(path)
    service = build_service(path)
    try:
        async with client(service) as api:
            response = await api.post(
                "/cases/case_1/beta-acceptance",
                headers=ALICE,
                json={"terms_accepted": True, "disclosure_sha256": "0" * 64},
            )
            assert response.status_code == 409
            assert response.json() == {"detail": "beta_disclosure_changed"}
            assert counts(service._store)["pb_cases"] == 0
    finally:
        service._store.close()


async def test_expired_entitlement_refuses_the_read_and_the_start(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    selected(path, eligible_until=NOW + timedelta(days=1))
    service = build_service(path, clock=Clock(NOW + timedelta(days=2)))
    try:
        async with client(service) as api:
            for response in (
                await api.get("/cases/case_1/beta-offer", headers=ALICE),
                await accept(api),
            ):
                assert response.status_code == 409
                assert response.json() == {"detail": "beta_qualification_expired"}
            assert counts(service._store)["pb_cases"] == 0
    finally:
        service._store.close()


async def test_current_customer_age_or_rental_stop_refuses_before_free_work(
    tmp_path: Path,
) -> None:
    cases = (
        (INTAKE, Clock(NOW + timedelta(days=7))),
        ({**INTAKE, "rental_company_paid": True}, Clock(NOW)),
    )
    for number, (intake, clock) in enumerate(cases, start=1):
        path = tmp_path / f"pb{number}.sqlite"
        selected(path, intake=intake, eligible_until=NOW + timedelta(days=30))
        service = build_service(path, clock=clock)
        try:
            async with client(service) as api:
                response = await accept(api)
                assert response.status_code == 409
                assert response.json() == {"detail": "case_stopped"}
                assert counts(service._store)["pb_cases"] == 0
        finally:
            service._store.close()


async def test_changed_bound_input_agency_or_paid_attempt_refuses(tmp_path: Path) -> None:
    mutations = (
        (
            "intake_json",
            json.dumps({**INTAKE, "customer_position": {"statement": "Changed facts."}}),
        ),
        ("prehold_json", offer_json(1).replace("Certified mail", "Changed mail")),
        (
            "agency_json",
            agency_json(
                ResolvedAgency(
                    "CA:issuer-1.gov",
                    "Changed issuer",
                    "CA",
                    ("issuer-1.gov",),
                    IssuerType.MUNICIPAL,
                    "Parking review",
                    "CA",
                )
            ),
        ),
    )
    for index, (column, value) in enumerate(mutations):
        path = tmp_path / f"changed{index}.sqlite"
        selected(path)
        with SqliteTransactionStore(path) as store:
            store._require_open().execute(
                f"UPDATE pb_intake_records SET {column}=? WHERE intake_id='case_1'", (value,)
            )
        service = build_service(path)
        try:
            async with client(service) as api:
                response = await accept(api)
                assert response.status_code == 409
                assert response.json() == {"detail": "beta_qualification_changed"}
                assert counts(service._store)["pb_cases"] == 0
        finally:
            service._store.close()

    path = tmp_path / "paid.sqlite"
    selected(path)
    with SqliteTransactionStore(path) as store:
        store._require_open().execute(
            "INSERT INTO pb_intake_authorization_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "case_1",
                1,
                "paid-attempt",
                "CERTIFIED",
                2999,
                "pm_fixture",
                "{}",
                "pending",
                None,
                None,
                None,
                NOW.isoformat(),
                NOW.isoformat(),
                1,
            ),
        )
    service = build_service(path)
    try:
        async with client(service) as api:
            response = await accept(api)
            assert response.status_code == 409
            assert response.json() == {"detail": "beta_payment_attempt_exists"}
            assert counts(service._store)["pb_cases"] == 0
    finally:
        service._store.close()


def test_concurrent_acceptance_has_one_case_and_one_receipt(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    selected(path)
    barrier = Barrier(2)

    def run(_: int) -> s.BetaAcceptanceReceipt:
        service = build_service(path)
        try:
            barrier.wait(timeout=10)
            return service.accept_beta(
                "case_1",
                s.AcceptBeta(terms_accepted=True, disclosure_sha256=BETA_DISCLOSURE_SHA256),
            )
        finally:
            service._store.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        receipts = list(workers.map(run, (1, 2)))
    assert receipts[0] == receipts[1]
    with SqliteTransactionStore(path) as store:
        row = store._require_open().execute("SELECT COUNT(*) FROM pb_cases").fetchone()
        assert row == (1,)
        entitlement = store.beta_entitlement("case_1")
        assert entitlement is not None
        assert entitlement.accepted_at == NOW


def test_accepted_beta_refuses_paid_authorization_and_unapproved_mailing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pb.sqlite"
    selected(path)
    service = build_service(path)
    try:
        service.accept_beta(
            "case_1", s.AcceptBeta(terms_accepted=True, disclosure_sha256=BETA_DISCLOSURE_SHA256)
        )
        with pytest.raises(ServiceConflict, match="beta_no_payment_required"):
            service.authorize(
                "case_1",
                s.StartAuthorization(terms_accepted=True, payment_method_id="pm_fixture"),
            )
        entitlement = service._store.beta_entitlement("case_1")
        assert entitlement is not None
        assert entitlement.case_token is not None
        with pytest.raises(ServiceConflict, match="beta_no_payment_required"):
            service._authorization("case_1", entitlement.case_token)
        with pytest.raises(ServiceConflict, match="adopt_current_text_first"):
            service.approve(
                "case_1",
                s.Approve(
                    adopted_sha256="a" * 64,
                    packet_sha256="b" * 64,
                    envelope_sha256="c" * 64,
                ),
            )
        assert counts(service._store)["pb_mail_attempts"] == 0
        assert counts(service._store)["pb_submission_operations"] == 0
        assert counts(service._store)["pb_payment_operations"] == 0
    finally:
        service._store.close()


def test_only_selected_candidates_can_accept_and_batch_stays_closed(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    with SqliteTransactionStore(path) as store:
        store.create_beta_campaign(
            "beta-1", START, END, "identity-fixture-v1", "owner-fixture", NOW
        )
        create_candidate(store, 8, owner="owner-8", jurisdiction=1)
        for number in range(1, 8):
            create_candidate(store, number, owner=f"owner-{number}")
        batch = store.select_beta_batch(
            "beta-1", "batch-1", "approval-1", NOW, randbelow=lambda _: 0
        )
        assert {item.intake_id for item in batch} == {f"case_{number}" for number in range(1, 6)}
        assert store.beta_entitlement("case_8") is None
        with pytest.raises(BetaConflict, match="review_required"):
            store.select_beta_batch("beta-1", "batch-2", "approval-2", NOW)
    service = build_service(path)
    try:
        for number in range(6, 9):
            with pytest.raises(ServiceConflict, match="beta_entitlement_required"):
                service.accept_beta(
                    f"case_{number}",
                    s.AcceptBeta(terms_accepted=True, disclosure_sha256=BETA_DISCLOSURE_SHA256),
                )
        for number in range(1, 6):
            service.accept_beta(
                f"case_{number}",
                s.AcceptBeta(terms_accepted=True, disclosure_sha256=BETA_DISCLOSURE_SHA256),
            )
        assert counts(service._store)["pb_cases"] == 5
        assert counts(service._store)["pb_beta_entitlements"] == 5
    finally:
        service._store.close()


@pytest.mark.parametrize("operation", ["accept_beta", "beta_offer"])
def test_delayed_snapshot_reconciles_a_committed_acceptance(tmp_path: Path, operation: str) -> None:
    path = tmp_path / "pb.sqlite"
    selected(path)
    expires = NOW + timedelta(days=1)
    winner = build_service(path, clock=Clock(expires - timedelta(microseconds=1)))
    body = s.AcceptBeta(terms_accepted=True, disclosure_sha256=BETA_DISCLOSURE_SHA256)
    committed: list[s.BetaAcceptanceReceipt] = []

    def delayed_clock() -> datetime:
        committed.append(winner.accept_beta("case_1", body))
        return expires

    delayed = build_service(path, clock=delayed_clock)
    try:
        if operation == "accept_beta":
            receipt = delayed.accept_beta("case_1", body)
            assert receipt == committed[0]
        else:
            offer = delayed.beta_offer("case_1")
            assert offer.accepted_at == committed[0].accepted_at
        assert delayed._store._require_open().execute(
            "SELECT COUNT(*) FROM pb_cases"
        ).fetchone() == (1,)
        entitlement = delayed._store.beta_entitlement("case_1")
        assert entitlement is not None
        assert entitlement.accepted_at == committed[0].accepted_at
    finally:
        winner._store.close()
        delayed._store.close()


def test_reconciled_acceptance_still_refuses_a_wrong_disclosure(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    selected(path)
    expires = NOW + timedelta(days=1)
    winner = build_service(path, clock=Clock(expires - timedelta(microseconds=1)))
    correct = s.AcceptBeta(terms_accepted=True, disclosure_sha256=BETA_DISCLOSURE_SHA256)
    committed: list[s.BetaAcceptanceReceipt] = []

    def delayed_clock() -> datetime:
        committed.append(winner.accept_beta("case_1", correct))
        return expires

    delayed = build_service(path, clock=delayed_clock)
    try:
        with pytest.raises(ServiceConflict, match="beta_disclosure_changed"):
            delayed.accept_beta(
                "case_1",
                s.AcceptBeta(terms_accepted=True, disclosure_sha256="0" * 64),
            )
        row = delayed._store._require_open().execute("SELECT COUNT(*) FROM pb_cases").fetchone()
        assert row == (1,)
    finally:
        winner._store.close()
        delayed._store.close()
