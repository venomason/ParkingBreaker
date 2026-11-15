"""Accepted-beta customer-reported notice metadata and private page retrieval (F02)."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from httpx2 import ASGITransport, AsyncClient

from parkingbreaker import case_state as cs
from parkingbreaker.api.app import create_app
from parkingbreaker.api.customer_auth import CSRF_COOKIE, AuthHttp
from parkingbreaker.beta_store import BETA_DISCLOSURE_SHA256
from parkingbreaker.blob_store import FakeBlobStore, LocalBlobStore, photo_ref
from parkingbreaker.case_hold_store import HoldCommand, HoldReason
from parkingbreaker.case_service import CaseService
from parkingbreaker.customer_access import AccessService, token_digest
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_beta_acceptance_api import (
    ALICE,
    BOB,
    build_service,
    client,
    counts,
    create_candidate,
    selected,
)
from test_beta_admission import END, NOW, START
from test_blob_store import png

pytestmark = pytest.mark.anyio
ORIGIN = "https://portal.example.test"


def notice_body(refs: list[str], **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": "notice-request-1",
        "pages": [
            {"role": role, "photo_ref": ref}
            for role, ref in zip(("envelope", "page"), refs, strict=False)
        ],
        "customer_seen_on": "2026-10-02",
        "printed_on": "2026-10-01",
        "stated_deadline_text": "Hearing requested by 11/15/2026",
    }
    body.update(overrides)
    return body


async def upload(api: AsyncClient, workspace: str = "ws_1", color: str = "red") -> str:
    response = await api.post(
        f"/workspaces/{workspace}/photos",
        headers=ALICE,
        json={
            "content_type": "image/png",
            "data_base64": __import__("base64").b64encode(png(color=color)).decode(),
        },
    )
    assert response.status_code == 201, response.text
    ref = response.json()["photo_ref"]
    assert isinstance(ref, str)
    return ref


async def accepted(api: AsyncClient) -> None:
    response = await api.post(
        "/cases/case_1/beta-acceptance",
        headers=ALICE,
        json={"terms_accepted": True, "disclosure_sha256": BETA_DISCLOSURE_SHA256},
    )
    assert response.status_code == 200, response.text


async def test_accepted_notice_metadata_list_page_replay_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    selected(path)
    service = build_service(path)
    try:
        async with client(service) as api:
            await accepted(api)
            envelope = await upload(api)
            page_ref = await upload(api, color="blue")
            body = notice_body([envelope, page_ref])
            created = await api.post("/cases/case_1/notices", headers=ALICE, json=body)
            assert created.status_code == 201, created.text
            receipt = created.json()
            assert receipt["case_id"] == "case_1"
            assert receipt["request_id"] == "notice-request-1"
            assert receipt["pages"] == [
                {"page_number": 1, "role": "envelope"},
                {"page_number": 2, "role": "page"},
            ]
            assert receipt["reported_fields"] == {
                "customer_seen_on": "2026-10-02",
                "printed_on": "2026-10-01",
                "stated_deadline_text": "Hearing requested by 11/15/2026",
            }
            assert receipt["correction_of"] is None
            assert receipt["needs_confirmation"] is True
            assert receipt["review_state"] == "unreviewed"
            assert not {"owner", "case_token", "payload_sha256", "photo_ref"} & set(receipt)
            assert created.headers["cache-control"] == "no-store"
            assert (
                await api.post("/cases/case_1/notices", headers=ALICE, json=body)
            ).json() == receipt

            listing = await api.get("/cases/case_1/notices", headers=ALICE)
            assert listing.status_code == 200
            assert listing.headers["cache-control"] == "no-store"
            assert listing.json() == {"notices": [receipt], "next_cursor": None}

            page = await api.get(
                f"/cases/case_1/notices/{receipt['notice_id']}/pages/1", headers=ALICE
            )
            assert page.status_code == 200
            assert page.headers["content-type"] == "image/png"
            assert page.headers["x-content-type-options"] == "nosniff"
            assert page.headers["cache-control"] == "no-store"
            assert photo_ref(page.content) == envelope

            wrong = copy.deepcopy(body)
            wrong["stated_deadline_text"] = "different"
            conflict = await api.post("/cases/case_1/notices", headers=ALICE, json=wrong)
            assert conflict.status_code == 409
            assert conflict.json() == {"detail": "beta_feedback_request_conflict"}

            # Foreign owners and unknown pages never leak existence.
            assert (await api.get("/cases/case_1/notices", headers=BOB)).status_code == 404
            assert (
                await api.get(
                    f"/cases/case_1/notices/{receipt['notice_id']}/pages/3", headers=ALICE
                )
            ).status_code == 409
        photo_items = dict(cast(FakeBlobStore, service._deps.photos).items)
    finally:
        service._store.close()

    reopened = build_service(path)
    cast(FakeBlobStore, reopened._deps.photos).items.update(photo_items)
    try:
        async with client(reopened) as api:
            replay = await api.post(
                "/cases/case_1/notices", headers=ALICE, json=notice_body([envelope, page_ref])
            )
            assert replay.status_code == 201
            assert replay.json() == receipt
    finally:
        reopened._store.close()


async def test_pending_selection_foreign_case_and_bad_refs_are_refused(tmp_path: Path) -> None:
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
            ref = await upload(api)
            missing = "photo_" + "a" * 64
            refused = await api.post(
                "/cases/case_1/notices", headers=ALICE, json=notice_body([ref])
            )
            assert refused.status_code == 409
            assert refused.json() == {"detail": "beta_feedback_accepted_beta_required"}
            await accepted(api)
            refused = await api.post(
                "/cases/case_1/notices", headers=ALICE, json=notice_body([ref, missing])
            )
            assert refused.status_code == 409
            assert refused.json() == {"detail": "beta_feedback_photo_missing"}
            rows = (
                service._store._require_open()
                .execute("SELECT COUNT(*) FROM pb_beta_feedback_notices")
                .fetchone()
            )
            assert rows == (0,)
            foreign = await api.post(
                "/cases/case_2/notices", headers=ALICE, json=notice_body([ref])
            )
            assert foreign.status_code == 404
            malformed = await api.post(
                "/cases/case_1/notices",
                headers=ALICE,
                json={**notice_body([ref]), "owner": "alice"},
            )
            assert malformed.status_code == 422
            assert malformed.json() == {"detail": "invalid_request"}
    finally:
        service._store.close()


async def test_pagination_cursor_saved_order_and_tampered_bytes(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    selected(path)
    service = build_service(path)
    try:
        async with client(service) as api:
            await accepted(api)
            first_ref = await upload(api, color="red")
            second_ref = await upload(api, color="blue")
            first = await api.post(
                "/cases/case_1/notices", headers=ALICE, json=notice_body([first_ref])
            )
            second = await api.post(
                "/cases/case_1/notices",
                headers=ALICE,
                json=notice_body([second_ref], request_id="notice-request-2"),
            )
            assert first.status_code == second.status_code == 201
            page_one = await api.get("/cases/case_1/notices?limit=1", headers=ALICE)
            assert page_one.status_code == 200
            assert [item["request_id"] for item in page_one.json()["notices"]] == [
                "notice-request-1"
            ]
            cursor = page_one.json()["next_cursor"]
            assert isinstance(cursor, str)
            page_two = await api.get(
                "/cases/case_1/notices", headers=ALICE, params={"limit": 1, "cursor": cursor}
            )
            assert page_two.status_code == 200
            assert page_two.json()["notices"][0]["request_id"] == "notice-request-2"
            assert page_two.json()["next_cursor"] is None

            # Saved order is stable even when the bytes behind the ref are tampered with.
            cast(FakeBlobStore, service._deps.photos).items[("alice", first_ref)] = png(
                color="green"
            )
            tampered = await api.get(
                f"/cases/case_1/notices/{first.json()['notice_id']}/pages/1", headers=ALICE
            )
            assert tampered.status_code == 409
            assert tampered.json() == {"detail": "beta_feedback_page_not_found"}
            unknown = await api.get(f"/cases/case_1/notices/{'0' * 32}/pages/1", headers=ALICE)
            assert unknown.status_code == 409
            bad_cursor = await api.get(
                "/cases/case_1/notices", headers=ALICE, params={"cursor": "not-a-cursor"}
            )
            assert bad_cursor.status_code == 409
            assert bad_cursor.json() == {"detail": "invalid_beta_feedback_cursor"}
    finally:
        service._store.close()


# Real customer-cookie/session/CSRF coverage with a real temporary SQLite and disk blobs ----
class _NoMail:
    def queue_link(self, **_: object) -> None:
        return None


def _credential(value: int) -> str:
    return base64.urlsafe_b64encode(bytes([value]) * 32).decode("ascii").rstrip("=")


def _real_app(
    service: CaseService, store: SqliteTransactionStore, clock: Any, *, start: int = 101
) -> Any:
    generated = iter(range(start, start + 200))
    access = AccessService(store, clock=clock, token_factory=lambda: _credential(next(generated)))
    return create_app(
        service,
        customer_auth=AuthHttp(access, _NoMail(), origin=ORIGIN, clock=clock),
    )


async def _authenticate(
    api: AsyncClient,
    store: SqliteTransactionStore,
    customer: str,
    credential_value: int,
    clock: Any,
) -> dict[str, str]:
    raw = _credential(credential_value)
    store.issue_identity_link(
        customer_id=customer,
        email=f"{customer}@example.test",
        token_digest=token_digest(raw),
        issued_at=clock(),
        expires_at=clock() + timedelta(minutes=15),
    )
    response = await api.post("/auth/confirm", headers={"Origin": ORIGIN}, json={"token": raw})
    assert response.status_code == 200, response.text
    return {"Origin": ORIGIN, "X-CSRF-Token": api.cookies[CSRF_COOKIE]}


def _local_service(path: Path, root: Path, clock: Any) -> CaseService:
    base = build_service(path, clock=clock)
    return CaseService(replace(base._deps, photos=LocalBlobStore(root)))


def _notice_count(store: SqliteTransactionStore) -> int:
    row = store._require_open().execute("SELECT COUNT(*) FROM pb_beta_feedback_notices").fetchone()
    assert row is not None
    return int(row[0])


def _disk_path(root: Path, owner: str, ref: str) -> Path:
    return root / hashlib.sha256(owner.encode()).hexdigest() / ref


async def _upload(api: AsyncClient, headers: dict[str, str], workspace: str, color: str) -> str:
    response = await api.post(
        f"/workspaces/{workspace}/photos",
        headers=headers,
        json={
            "content_type": "image/png",
            "data_base64": base64.b64encode(png(color=color)).decode(),
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["photo_ref"])


async def _accepted_cookie(api: AsyncClient, headers: dict[str, str], case: str = "case_1") -> None:
    response = await api.post(
        f"/cases/{case}/beta-acceptance",
        headers=headers,
        json={"terms_accepted": True, "disclosure_sha256": BETA_DISCLOSURE_SHA256},
    )
    assert response.status_code == 200, response.text


async def test_real_cookie_csrf_local_disk_public_cursor_and_strict_dates(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    path = tmp_path / "pb.sqlite"
    root = tmp_path / "blobs"
    selected(path)
    clock = [NOW]
    service = _local_service(path, root, lambda: clock[0])
    store = service._store
    app = _real_app(service, store, lambda: clock[0])
    try:
        anonymous = AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN)
        await anonymous.__aenter__()
        try:
            assert (await anonymous.get("/cases/case_1/notices")).status_code == 401
            bearer_only = await anonymous.get("/cases/case_1/notices", headers=ALICE)
            assert bearer_only.status_code == 401
            assert (await anonymous.post("/cases/case_1/notices", json={})).status_code in {
                401,
                422,
            }
        finally:
            await anonymous.aclose()

        api = AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN)
        await api.__aenter__()
        try:
            headers = await _authenticate(api, store, "alice", 1, lambda: clock[0])
            await _accepted_cookie(api, headers)
            envelope = await _upload(api, headers, "ws_1", "red")
            page_ref = await _upload(api, headers, "ws_1", "blue")
            body = notice_body([envelope, page_ref])

            # Missing CSRF and wrong/missing origin are refused before any row exists.
            assert _notice_count(store) == 0
            no_csrf = await api.post("/cases/case_1/notices", headers={"Origin": ORIGIN}, json=body)
            assert no_csrf.status_code == 403
            no_origin = await api.post(
                "/cases/case_1/notices",
                headers={"X-CSRF-Token": headers["X-CSRF-Token"]},
                json=body,
            )
            assert no_origin.status_code == 403
            assert _notice_count(store) == 0

            # R1: only a canonical JSON YYYY-MM-DD string (or null) is accepted.
            canary = "CANARY-CUSTOMER-TEXT-7f31"
            for bad in ("2026-1-1", "2026-10-02T00:00:00", 20261002, "2026-02-30", "2026-13-01"):
                rejected = await api.post(
                    "/cases/case_1/notices",
                    headers=headers,
                    json={**body, "customer_seen_on": bad, "stated_deadline_text": canary},
                )
                assert rejected.status_code == 422
                assert rejected.json() == {"detail": "invalid_request"}
                assert canary not in rejected.text
            assert canary not in caplog.text
            assert _notice_count(store) == 0

            created = await api.post("/cases/case_1/notices", headers=headers, json=body)
            assert created.status_code == 201, created.text
            receipt = created.json()
            encoded = json.dumps(receipt)
            for forbidden in ("bfc_", "owner", "photo_ref", "payload_sha256", "case_token"):
                assert forbidden not in encoded
            assert receipt["reported_fields"]["customer_seen_on"] == "2026-10-02"

            page = await api.get(
                f"/cases/case_1/notices/{receipt['notice_id']}/pages/1", headers=headers
            )
            assert page.status_code == 200
            assert page.headers["content-type"] == "image/png"
            assert page.headers["x-content-type-options"] == "nosniff"
            assert page.headers["cache-control"] == "no-store"
            assert photo_ref(page.content) == envelope

            second_ref = await _upload(api, headers, "ws_1", "green")
            second = await api.post(
                "/cases/case_1/notices",
                headers=headers,
                json=notice_body([second_ref], request_id="notice-request-2"),
            )
            assert second.status_code == 201
            # The public cursor is the last notice id, never the internal bfc_ value.
            first_page = await api.get(
                "/cases/case_1/notices", headers=headers, params={"limit": 1}
            )
            assert first_page.json()["next_cursor"] == receipt["notice_id"]
            assert "bfc_" not in json.dumps(first_page.json())
            resumed = await api.get(
                "/cases/case_1/notices",
                headers=headers,
                params={"limit": 1, "cursor": receipt["notice_id"]},
            )
            assert [item["request_id"] for item in resumed.json()["notices"]] == [
                "notice-request-2"
            ]
            for bad_cursor in ("not-a-cursor", "0" * 32, receipt["notice_id"].upper()):
                refused = await api.get(
                    "/cases/case_1/notices", headers=headers, params={"cursor": bad_cursor}
                )
                assert refused.status_code == 409
                assert refused.json() == {"detail": "invalid_beta_feedback_cursor"}

            # Actual disk bytes must be present and hash-exact before INSERT and page read.
            tampered = await _upload(api, headers, "ws_1", "yellow")
            _disk_path(root, "alice", tampered).write_bytes(png(color="purple"))
            tampered_post = await api.post(
                "/cases/case_1/notices",
                headers=headers,
                json=notice_body([tampered], request_id="notice-tampered"),
            )
            assert tampered_post.status_code == 409
            assert tampered_post.json() == {"detail": "beta_feedback_photo_missing"}

            missing = await _upload(api, headers, "ws_1", "black")
            _disk_path(root, "alice", missing).unlink()
            missing_post = await api.post(
                "/cases/case_1/notices",
                headers=headers,
                json=notice_body([missing], request_id="notice-missing"),
            )
            assert missing_post.status_code == 409
            assert missing_post.json() == {"detail": "beta_feedback_photo_missing"}
            assert _notice_count(store) == 2

            _disk_path(root, "alice", envelope).unlink()
            gone = await api.get(
                f"/cases/case_1/notices/{receipt['notice_id']}/pages/1", headers=headers
            )
            assert gone.status_code == 409
            assert gone.json() == {"detail": "beta_feedback_page_not_found"}
        finally:
            await api.aclose()
    finally:
        service._store.close()


async def test_two_owners_same_bytes_second_case_and_notice_only_state(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    root = tmp_path / "blobs"
    with SqliteTransactionStore(path) as store:
        store.create_beta_campaign(
            "beta-1", START, END, "identity-fixture-v1", "owner-fixture", NOW
        )
        create_candidate(store, 1, owner="alice")
        create_candidate(store, 3, owner="bob")
        store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)
    clock = [NOW]
    service = _local_service(path, root, lambda: clock[0])
    store = service._store
    app = _real_app(service, store, lambda: clock[0])
    try:
        alice = AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN)
        bob = AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN)
        await alice.__aenter__()
        await bob.__aenter__()
        try:
            alice_headers = await _authenticate(alice, store, "alice", 1, lambda: clock[0])
            bob_headers = await _authenticate(bob, store, "bob", 2, lambda: clock[0])
            await _accepted_cookie(alice, alice_headers, "case_1")
            await _accepted_cookie(bob, bob_headers, "case_3")

            bob_ref = await _upload(bob, bob_headers, "ws_3", "red")
            # Same bytes owned only by Bob are invisible to Alice and write no notice.
            foreign = await alice.post(
                "/cases/case_1/notices",
                headers=alice_headers,
                json=notice_body([bob_ref]),
            )
            assert foreign.status_code == 409
            assert foreign.json() == {"detail": "beta_feedback_photo_missing"}
            assert _notice_count(store) == 0

            # Independent ownership: Alice's own upload of the same bytes is distinct.
            alice_ref = await _upload(alice, alice_headers, "ws_1", "red")
            assert alice_ref == bob_ref
            case_before = store.get_case("case_1")
            entitlement_before = store.beta_entitlement("case_1")
            counters_before = counts(store)
            created = await alice.post(
                "/cases/case_1/notices", headers=alice_headers, json=notice_body([alice_ref])
            )
            assert created.status_code == 201, created.text
            notice_id = created.json()["notice_id"]
            assert store.get_case("case_1") == case_before
            assert store.beta_entitlement("case_1") == entitlement_before
            assert counts(store) == counters_before
            assert _notice_count(store) == 1

            # A notice id from Alice's case cannot be used against Bob's case.
            wrong_case_cursor = await alice.get(
                "/cases/case_3/notices",
                headers=alice_headers,
                params={"cursor": notice_id},
            )
            assert wrong_case_cursor.status_code == 404
            wrong_case_page = await alice.get(
                f"/cases/case_3/notices/{notice_id}/pages/1", headers=alice_headers
            )
            assert wrong_case_page.status_code == 404
            foreign_list = await bob.get(
                "/cases/case_1/notices",
                headers=bob_headers,
                params={"cursor": notice_id},
            )
            assert foreign_list.status_code == 404

            # Bob may independently record the same content-addressed bytes for his case.
            bob_notice = await bob.post(
                "/cases/case_3/notices", headers=bob_headers, json=notice_body([bob_ref])
            )
            assert bob_notice.status_code == 201, bob_notice.text
            assert (await bob.get("/cases/case_1/notices", headers=bob_headers)).status_code == 404
        finally:
            await alice.aclose()
            await bob.aclose()
    finally:
        service._store.close()


def _paid_case(store: SqliteTransactionStore) -> Any:
    """A genuine linked paid intake/case owned by Alice; never a fake beta flag."""
    store.create_owned_workspace("alice", "ws_paid", Workspace(), NOW)
    paid = store.start_case("ws_paid", "paid-authorization", NOW, NOW + timedelta(days=5))
    intake = store.record_intake(
        "case_paid",
        "ws_paid",
        json.dumps({"fixture": "paid"}),
        json.dumps({"kind": "offer", "issuer": {"issuer_id": "CA:issuer-paid.gov"}, "tiers": []}),
        NOW,
    )
    store.update_intake(
        "case_paid", intake.revision, selected_tier="CERTIFIED", case_token=paid.case_token
    )
    return paid


_STATE_TABLES = (
    "pb_cases",
    "pb_case_history",
    "pb_workspaces",
    "pb_workspace_history",
    "pb_intake_records",
    "pb_intake_owners",
    "pb_beta_campaigns",
    "pb_beta_jurisdiction_aliases",
    "pb_beta_candidates",
    "pb_beta_batches",
    "pb_beta_entitlements",
    "pb_case_holds",
    "pb_case_hold_commands",
    "pb_pending_effects",
    "pb_coverage_counters",
    "pb_intake_authorization_attempts",
    "pb_payment_operations",
    "pb_payment_cursor",
    "pb_mail_attempts",
    "pb_mail_destination_bindings",
    "pb_submission_operations",
    "pb_submission_cursors",
)


def _store_state(store: SqliteTransactionStore) -> dict[str, list[tuple[Any, ...]]]:
    """Snapshot the case, packet, approval, hold, payment and effect tables."""
    database = store._require_open()
    return {name: database.execute(f'SELECT * FROM "{name}"').fetchall() for name in _STATE_TABLES}


async def test_genuine_paid_case_refused_without_notice_or_payment_effect_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pb.sqlite"
    root = tmp_path / "blobs"
    with SqliteTransactionStore(path) as store:
        paid = _paid_case(store)
        assert paid.payment_state is PaymentState.AUTHORIZED
        assert paid.amount_cents == 2999
    clock = [NOW]
    service = _local_service(path, root, lambda: clock[0])
    store = service._store
    app = _real_app(service, store, lambda: clock[0])
    api = AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN)
    await api.__aenter__()
    try:
        headers = await _authenticate(api, store, "alice", 7, lambda: clock[0])
        ref = await _upload(api, headers, "ws_paid", "red")
        body = notice_body([ref], request_id="paid-notice-request")
        before = _store_state(store)

        refused = await api.post("/cases/case_paid/notices", headers=headers, json=body)
        assert refused.status_code == 409
        assert refused.json() == {"detail": "beta_feedback_no_charge_required"}
        listing = await api.get("/cases/case_paid/notices", headers=headers)
        assert listing.status_code == 409
        assert listing.json() == {"detail": "beta_feedback_no_charge_required"}
        page = await api.get(f"/cases/case_paid/notices/{'0' * 32}/pages/1", headers=headers)
        assert page.status_code == 409
        # A paid case has no retrievable notice; the fixed refusal never leaks one.
        assert page.json() == {"detail": "beta_feedback_notice_not_found"}

        assert _notice_count(store) == 0
        assert _store_state(store) == before
    finally:
        await api.aclose()
        service._store.close()


async def test_held_terminal_and_closed_campaign_accepted_beta_still_records(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pb.sqlite"
    root = tmp_path / "blobs"
    selected(path)
    clock = [NOW]
    service = _local_service(path, root, lambda: clock[0])
    store = service._store
    app = _real_app(service, store, lambda: clock[0])
    api = AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN)
    await api.__aenter__()
    try:
        headers = await _authenticate(api, store, "alice", 8, lambda: clock[0])
        await _accepted_cookie(api, headers)
        entitlement = store.beta_entitlement("case_1")
        assert entitlement is not None
        assert entitlement.case_token is not None
        case = store.get_case(entitlement.case_token)
        assert case is not None
        store.record_hold_command(
            "hold-1",
            case.case_token,
            HoldCommand.HOLD,
            NOW,
            expected_generation=None,
            reason=HoldReason.INCIDENT,
            review_due_at=NOW + timedelta(days=1),
        )
        held_state = (
            store.get_case(case.case_token),
            store.transition_history(case.case_token),
            store.effects(case.case_token),
            store.payment_cursor(case.case_token),
            store.beta_entitlement("case_1"),
            store.case_hold(case.case_token),
            _store_state(store),
        )
        envelope = await _upload(api, headers, "ws_1", "red")
        held = await api.post(
            "/cases/case_1/notices", headers=headers, json=notice_body([envelope])
        )
        assert held.status_code == 201, held.text
        assert (
            store.get_case(case.case_token),
            store.transition_history(case.case_token),
            store.effects(case.case_token),
            store.payment_cursor(case.case_token),
            store.beta_entitlement("case_1"),
            store.case_hold(case.case_token),
            _store_state(store),
        ) == held_state

        case = store.apply_letter_event(
            case.case_token,
            cs.Stopped(cs.StopReason.NOT_YET_VERIFIED, hard_stop=cs.HardStop.S2),
            case.revision,
            occurred_at=NOW,
        )
        assert case.letter_state is cs.CaseState.STOPPED_NO_CHARGE
        terminal_state = (
            store.get_case(case.case_token),
            store.transition_history(case.case_token),
            store.effects(case.case_token),
            store.payment_cursor(case.case_token),
            store.beta_entitlement("case_1"),
            store.case_hold(case.case_token),
            _store_state(store),
        )
        clock[0] = END + timedelta(days=1)
        with pytest.raises(ValueError, match="campaign_window_closed"):
            store.select_beta_batch("beta-1", "batch-2", "approval-2", clock[0])
        page_ref = await _upload(api, headers, "ws_1", "blue")
        terminal = await api.post(
            "/cases/case_1/notices",
            headers=headers,
            json=notice_body([page_ref], request_id="notice-terminal"),
        )
        assert terminal.status_code == 201, terminal.text
        listing = await api.get("/cases/case_1/notices", headers=headers)
        assert listing.status_code == 200
        assert [item["request_id"] for item in listing.json()["notices"]] == [
            "notice-request-1",
            "notice-terminal",
        ]
        page = await api.get(
            f"/cases/case_1/notices/{held.json()['notice_id']}/pages/1", headers=headers
        )
        assert page.status_code == 200
        assert page.content == png(color="red")
        assert (
            store.get_case(case.case_token),
            store.transition_history(case.case_token),
            store.effects(case.case_token),
            store.payment_cursor(case.case_token),
            store.beta_entitlement("case_1"),
            store.case_hold(case.case_token),
            _store_state(store),
        ) == terminal_state
    finally:
        await api.aclose()
        service._store.close()


async def test_disk_reopen_exact_replay_lost_response_and_changed_conflict(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pb.sqlite"
    root = tmp_path / "blobs"
    selected(path)
    clock = [NOW]
    service = _local_service(path, root, lambda: clock[0])
    store = service._store
    app = _real_app(service, store, lambda: clock[0])
    api = AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN)
    await api.__aenter__()
    try:
        headers = await _authenticate(api, store, "alice", 9, lambda: clock[0])
        await _accepted_cookie(api, headers)
        envelope = await _upload(api, headers, "ws_1", "red")
        page_ref = await _upload(api, headers, "ws_1", "blue")
        body = notice_body([envelope, page_ref])
        first = await api.post("/cases/case_1/notices", headers=headers, json=body)
        assert first.status_code == 201, first.text
        receipt = first.json()
        # The first response is discarded; only its durable outcome is replayed below.
        del first
    finally:
        await api.aclose()
        service._store.close()

    reopened = _local_service(path, root, lambda: clock[0])
    store = reopened._store
    app = _real_app(reopened, store, lambda: clock[0], start=200)
    api = AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN)
    await api.__aenter__()
    try:
        headers = await _authenticate(api, store, "alice", 10, lambda: clock[0])
        replay = await api.post("/cases/case_1/notices", headers=headers, json=body)
        assert replay.status_code == 201, replay.text
        assert replay.json() == receipt
        assert _notice_count(store) == 1
        listing = await api.get("/cases/case_1/notices", headers=headers)
        assert listing.status_code == 200
        assert listing.json() == {"notices": [receipt], "next_cursor": None}
        for number, expected in enumerate((png(color="red"), png(color="blue")), start=1):
            page = await api.get(
                f"/cases/case_1/notices/{receipt['notice_id']}/pages/{number}", headers=headers
            )
            assert page.status_code == 200
            assert page.content == expected
        changed = await api.post(
            "/cases/case_1/notices",
            headers=headers,
            json={**body, "stated_deadline_text": "different"},
        )
        assert changed.status_code == 409
        assert changed.json() == {"detail": "beta_feedback_request_conflict"}
        assert _notice_count(store) == 1
    finally:
        await api.aclose()
        reopened._store.close()


async def test_expired_session_wrong_csrf_and_wrong_origin_deny_before_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pb.sqlite"
    root = tmp_path / "blobs"
    selected(path)
    clock = [NOW]
    service = _local_service(path, root, lambda: clock[0])
    store = service._store
    app = _real_app(service, store, lambda: clock[0])
    api = AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN)
    await api.__aenter__()
    try:
        headers = await _authenticate(api, store, "alice", 11, lambda: clock[0])
        await _accepted_cookie(api, headers)
        ref = await _upload(api, headers, "ws_1", "red")
        body = notice_body([ref])
        assert _notice_count(store) == 0

        wrong_csrf = await api.post(
            "/cases/case_1/notices",
            headers={**headers, "X-CSRF-Token": _credential(99)},
            json=body,
        )
        assert wrong_csrf.status_code == 403
        assert _notice_count(store) == 0

        wrong_origin = await api.post(
            "/cases/case_1/notices",
            headers={**headers, "Origin": "https://evil.example.test"},
            json=body,
        )
        assert wrong_origin.status_code == 403
        assert _notice_count(store) == 0

        clock[0] = NOW + timedelta(days=31)
        assert (await api.get("/cases/case_1/notices", headers=headers)).status_code == 401
        expired = await api.post("/cases/case_1/notices", headers=headers, json=body)
        assert expired.status_code == 401
        assert _notice_count(store) == 0
    finally:
        await api.aclose()
        service._store.close()
