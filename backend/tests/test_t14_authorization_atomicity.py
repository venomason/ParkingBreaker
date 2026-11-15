"""Authorization atomicity and fault recovery tests (T14-02d)."""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parkingbreaker.api import schemas as s
from parkingbreaker.api.service import ServiceConflict
from parkingbreaker.case_service import AUTHORIZATION_REPLAY_LIMIT, CaseService
from parkingbreaker.intake_store import (
    AuthorizationAttempt,
    IntakeConflict,
    authorization_dispatch_refusal,
)
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.payment_records import PaymentOutcome, mutation_key
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_case_service import INTAKE, PNG_B64, service
from test_case_service import fake_resolution as fake_resolution
from test_t14_authorization_recovery import BODY, RecoveryCase, assert_dispatch_claim
from test_t14_authorization_recovery import recovery as recovery
from test_t14_intake_atomicity import HookedConnection

NOW = datetime(2026, 10, 15, 21, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 15, 22, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("elapsed", "updated", "reference", "state", "expected"),
    [
        (
            timedelta(microseconds=-1),
            timedelta(),
            None,
            "pending",
            "authorization_outcome_unresolved",
        ),
        (timedelta(), timedelta(), None, "pending", None),
        (timedelta(), timedelta(), None, "uncertain", None),
        (
            timedelta(seconds=1),
            timedelta(seconds=2),
            None,
            "uncertain",
            "authorization_outcome_unresolved",
        ),
        (timedelta(seconds=2), timedelta(seconds=2), None, "uncertain", None),
        (
            AUTHORIZATION_REPLAY_LIMIT - timedelta(microseconds=1),
            timedelta(),
            None,
            "pending",
            None,
        ),
        (
            AUTHORIZATION_REPLAY_LIMIT,
            timedelta(),
            None,
            "pending",
            "authorization_outcome_unresolved",
        ),
        (
            AUTHORIZATION_REPLAY_LIMIT,
            timedelta(),
            None,
            "uncertain",
            "authorization_outcome_unresolved",
        ),
        (timedelta(hours=25), timedelta(), "pi_known", "uncertain", None),
        (
            timedelta(seconds=1),
            timedelta(seconds=2),
            "pi_known",
            "uncertain",
            "authorization_outcome_unresolved",
        ),
        (timedelta(), timedelta(), "pi_declined", "declined", "card_not_authorized"),
    ],
)
def test_dispatch_refusal_is_a_pure_timestamp_and_state_decision(
    recovery: RecoveryCase,
    elapsed: timedelta,
    updated: timedelta,
    reference: str | None,
    state: str,
    expected: str | None,
) -> None:
    original = recovery.reserve()
    attempt = replace(
        original, updated_at=original.created_at + updated, authorization_ref=reference, state=state
    )
    assert (
        authorization_dispatch_refusal(
            attempt, original.created_at + elapsed, AUTHORIZATION_REPLAY_LIMIT
        )
        == expected
    )
    assert recovery.attempt == original


def test_dispatch_claim_commits_watermark_and_rejects_earlier_dispatch_or_outcome(
    recovery: RecoveryCase,
) -> None:
    saved = recovery.reserve()
    store = recovery.real._store
    now = saved.created_at + timedelta(seconds=2)
    claimed = store.claim_authorization_dispatch(
        recovery.case_id, recovery.key, now, AUTHORIZATION_REPLAY_LIMIT
    )
    assert_dispatch_claim(claimed, saved)
    assert claimed.updated_at == now
    recovery.restart()
    store = recovery.real._store
    assert recovery.attempt == claimed
    actions: tuple[Callable[[], AuthorizationAttempt], ...] = (
        lambda: store.claim_authorization_dispatch(
            recovery.case_id,
            recovery.key,
            now - timedelta(microseconds=1),
            AUTHORIZATION_REPLAY_LIMIT,
        ),
        lambda: store.record_authorization_outcome(
            recovery.case_id,
            recovery.key,
            "declined",
            "pi_stale",
            now - timedelta(microseconds=1),
        ),
    )
    for action in actions:
        with pytest.raises(IntakeConflict, match=r"^authorization_outcome_unresolved$"):
            action()
        assert recovery.attempt == claimed
        assert not store._require_open().in_transaction
    equal = store.claim_authorization_dispatch(
        recovery.case_id, recovery.key, now, AUTHORIZATION_REPLAY_LIMIT
    )
    assert_dispatch_claim(equal, claimed)
    recovery.assert_no_case()


def test_dispatch_claim_missing_or_terminal_attempts_do_not_write(recovery: RecoveryCase) -> None:
    store = recovery.real._store
    with pytest.raises(IntakeConflict, match=r"^authorization_attempt_not_found$"):
        store.claim_authorization_dispatch(
            recovery.case_id, recovery.key, recovery.clock(), AUTHORIZATION_REPLAY_LIMIT
        )
    assert not store._require_open().in_transaction
    recovery.reserve()
    declined = store.record_authorization_outcome(
        recovery.case_id, recovery.key, "declined", "pi_declined", recovery.clock()
    )
    with pytest.raises(IntakeConflict, match=r"^card_not_authorized$"):
        store.claim_authorization_dispatch(
            recovery.case_id, recovery.key, recovery.clock(), AUTHORIZATION_REPLAY_LIMIT
        )
    assert recovery.attempt == declined
    assert not store._require_open().in_transaction


def test_authorized_dispatch_claim_is_unchanged_even_with_earlier_clock(
    recovery: RecoveryCase,
) -> None:
    authorization = recovery.real.authorize(recovery.case_id, BODY)
    saved = recovery.attempt
    claimed = recovery.real._store.claim_authorization_dispatch(
        recovery.case_id,
        recovery.key,
        saved.created_at - timedelta(seconds=1),
        AUTHORIZATION_REPLAY_LIMIT,
    )
    assert claimed == saved
    assert recovery.attempt == saved
    assert not recovery.real._store._require_open().in_transaction
    recovery.assert_one_case(authorization)


@pytest.mark.parametrize("boundary", ["UPDATE pb_intake_authorization_attempts", "COMMIT"])
def test_dispatch_claim_failure_rolls_back_watermark(
    recovery: RecoveryCase,
    boundary: str,
) -> None:
    saved = recovery.reserve()
    store = recovery.real._store
    hooked = HookedConnection(store._require_open())
    store._database = hooked  # type: ignore[assignment]
    hooked.raise_on_statement = (boundary, RuntimeError("claim fault"))
    with pytest.raises(RuntimeError, match=r"^claim fault$"):
        store.claim_authorization_dispatch(
            recovery.case_id,
            recovery.key,
            saved.created_at + timedelta(seconds=2),
            AUTHORIZATION_REPLAY_LIMIT,
        )
    assert recovery.attempt == saved
    assert not hooked.in_transaction
    recovery.restart()
    assert recovery.attempt == saved
    assert recovery.payments.calls == []


class LockFakePaymentClient(FakePaymentClient):
    """Thread-safe fake with bounded locking and distinct holds per key."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    def authorize(
        self,
        amount_cents: int,
        payment_method_id: str,
        key: str,
        metadata: Mapping[str, str],
    ) -> PaymentOutcome:
        if not self._lock.acquire(timeout=10):
            raise TimeoutError("fake authorization lock timed out")
        try:
            if key not in self.authorizations:
                self.object = replace(
                    self.object, authorization_ref=f"pi_fake_{len(self.authorizations) + 1}"
                )
            return super().authorize(amount_cents, payment_method_id, key, metadata)
        finally:
            self._lock.release()


@pytest.mark.parametrize(
    "sql_boundary",
    [
        "INSERT INTO pb_cases",
        "INSERT INTO pb_pending_effects",
        "UPDATE pb_intake_records SET case_token",
        "UPDATE pb_intake_authorization_attempts SET state='authorized'",
        "COMMIT",
    ],
)
def test_a_complete_authorization_attempt_boundaries(tmp_path: Path, sql_boundary: str) -> None:
    db_path = tmp_path / f"test_a_{sql_boundary.replace(' ', '_')}.db"
    store = SqliteTransactionStore(db_path)
    iid, ws_token = "intake_a123", "ws_a123"
    stmt = "  Line 1: I parked lawfully at 10:00 AM.\nLine 2: Euro € test.  "
    photos, provs = ("photo_1.jpg", "photo_2.jpg"), frozenset(["prov_meter", "prov_sign"])
    prehold_json = json.dumps({"tiers": [{"tier": "CERTIFIED", "amount_cents": 2999}]})
    intake_json = json.dumps(
        {"statement": stmt, "photos": list(photos), "provisions": sorted(provs)}
    )

    try:
        store.create_owned_workspace("owner_alice", ws_token, Workspace(notes="notes"), NOW)
        store.record_intake_with_workspace_snapshot(
            iid, ws_token, intake_json, prehold_json, stmt, photos, provs, NOW
        )
        store.update_intake(iid, 1, selected_tier="CERTIFIED", case_token=None)

        req_dict = {
            "amount_cents": 2999,
            "currency": "usd",
            "metadata": {"case": iid, "tier": "CERTIFIED", "service_amount_cents": "2999"},
            "payment_method_id": "pm_first",
            "tier": "CERTIFIED",
        }
        req_json = json.dumps(req_dict, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = mutation_key("authorization", iid, "CERTIFIED:2999", "pm_first")

        store.begin_authorization_attempt(iid, key, "CERTIFIED", 2999, "pm_first", req_json, NOW)
        store.record_authorization_outcome(iid, key, "uncertain", "pi_Original123", NOW)

        hooked = HookedConnection(store._require_open())
        store._database = hooked  # type: ignore[assignment]
        if sql_boundary == "COMMIT":
            hooked.raise_before_commit = RuntimeError("Fault before COMMIT")
        else:
            hooked.raise_on_statement = (sql_boundary, RuntimeError(f"Fault on {sql_boundary}"))

        with pytest.raises(RuntimeError):
            store.complete_authorization_attempt(
                iid, key, "pi_Original123", NOW, NOW + timedelta(days=7)
            )
    finally:
        store.close()

    reopened = SqliteTransactionStore(db_path)
    try:
        conn = reopened._require_open()
        assert conn.execute("SELECT COUNT(*) FROM pb_cases").fetchone()[0] == 0
        intake = reopened.get_intake(iid)
        assert intake is not None
        assert intake.case_token is None
        att = reopened.get_authorization_attempt(iid, key)
        assert att is not None
        assert att.state == "uncertain"
        assert att.authorization_ref == "pi_Original123"
        assert conn.execute("SELECT COUNT(*) FROM pb_pending_effects").fetchone()[0] == 0

        ws = reopened.get_workspace(ws_token)
        assert ws is not None
        assert ws.workspace.position == stmt
        assert ws.workspace.photo_refs == photos
        assert ws.workspace.provision_selections == provs
        assert intake.intake_json == intake_json

        case = reopened.complete_authorization_attempt(
            iid, key, "pi_Original123", NOW, NOW + timedelta(days=7)
        )
        assert case is not None
        upd_intake = reopened.get_intake(iid)
        assert upd_intake is not None
        assert upd_intake.case_token == case.case_token
        upd_att = reopened.get_authorization_attempt(iid, key)
        assert upd_att is not None
        assert upd_att.state == "authorized"
        assert conn.execute("SELECT COUNT(*) FROM pb_pending_effects").fetchone()[0] == 1

        ws_after = reopened.get_workspace(ws_token)
        assert ws_after is not None
        assert ws_after.workspace.position == stmt
        assert ws_after.workspace.photo_refs == photos
        assert ws_after.workspace.provision_selections == provs
        assert upd_intake.intake_json == intake_json
    finally:
        reopened.close()


def test_b_lost_reply_after_commit(tmp_path: Path) -> None:
    db_path = tmp_path / "test_b.db"
    store = SqliteTransactionStore(db_path)
    iid, ws_token = "intake_b123", "ws_b123"
    stmt, photos, provs = "Statement B\nNewline", ("photo_b1.jpg",), frozenset(["prov_b"])
    prehold_json = json.dumps({"tiers": [{"tier": "CERTIFIED", "amount_cents": 2999}]})
    intake_json = json.dumps(
        {"statement": stmt, "photos": list(photos), "provisions": sorted(provs)}
    )

    try:
        store.create_owned_workspace("owner_bob", ws_token, Workspace(notes="notes_b"), NOW)
        store.record_intake_with_workspace_snapshot(
            iid, ws_token, intake_json, prehold_json, stmt, photos, provs, NOW
        )
        store.update_intake(iid, 1, selected_tier="CERTIFIED", case_token=None)

        req_dict = {
            "amount_cents": 2999,
            "currency": "usd",
            "metadata": {"case": iid, "tier": "CERTIFIED", "service_amount_cents": "2999"},
            "payment_method_id": "pm_first",
            "tier": "CERTIFIED",
        }
        req_json = json.dumps(req_dict, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = mutation_key("authorization", iid, "CERTIFIED:2999", "pm_first")

        store.begin_authorization_attempt(iid, key, "CERTIFIED", 2999, "pm_first", req_json, NOW)
        store.record_authorization_outcome(iid, key, "uncertain", "pi_Original123", NOW)

        hooked = HookedConnection(store._require_open())
        store._database = hooked  # type: ignore[assignment]
        hooked.raise_after_commit = RuntimeError("lost reply after commit")

        with pytest.raises(RuntimeError):
            store.complete_authorization_attempt(
                iid, key, "pi_Original123", NOW, NOW + timedelta(days=7)
            )
    finally:
        store.close()

    reopened = SqliteTransactionStore(db_path)
    try:
        conn = reopened._require_open()
        assert conn.execute("SELECT COUNT(*) FROM pb_cases").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM pb_pending_effects").fetchone()[0] == 1
        intake = reopened.get_intake(iid)
        assert intake is not None
        assert intake.case_token is not None
        case = reopened.get_case(intake.case_token)
        assert case is not None
        att = reopened.get_authorization_attempt(iid, key)
        assert att is not None
        assert att.state == "authorized"
        assert att.authorization_ref == "pi_Original123"

        retry_case = reopened.complete_authorization_attempt(
            iid, key, "pi_Original123", LATER, LATER + timedelta(days=7)
        )
        assert retry_case.case_token == case.case_token
        assert retry_case.authorized_at == case.authorized_at
        assert retry_case.authorization_expires_at == case.authorization_expires_at
        assert conn.execute("SELECT COUNT(*) FROM pb_pending_effects").fetchone()[0] == 1
        att_after = reopened.get_authorization_attempt(iid, key)
        assert att_after is not None
        assert att_after.revision == att.revision
    finally:
        reopened.close()


def test_c_concurrent_authorization_races(tmp_path: Path) -> None:
    db_same = tmp_path / "test_c_same.db"
    shared_payments = LockFakePaymentClient()
    svc_setup = service(db_same, payments=shared_payments)

    try:
        ws_v = svc_setup.create_workspace("owner_c1", s.CreateWorkspace(notes="notes_c1"))
        ws_id = ws_v.workspace_id
        pref = svc_setup.upload_photo(
            ws_id, s.PhotoUpload(content_type="image/png", data_base64=PNG_B64)
        ).photo_ref
        b_same = copy.deepcopy(INTAKE)
        b_same["photo_refs"] = [pref]
        receipt = svc_setup.submit(ws_id, s.SubmitIntake.model_validate(b_same))
        cid = receipt.case_id
        svc_setup.choose(cid, s.ChooseTier(tier=s.MailTier.CERTIFIED))
    finally:
        svc_setup._store.close()

    barrier_same = threading.Barrier(2)
    res_same: list[s.Authorization | None] = [None, None]
    exc_same: list[Exception | None] = [None, None]

    def worker_same(idx: int) -> None:
        svc_thread = service(db_same, payments=shared_payments)
        try:
            barrier_same.wait(timeout=10)
            res_same[idx] = svc_thread.authorize(
                cid, s.StartAuthorization(payment_method_id="pm_first", terms_accepted=True)
            )
        except Exception as e:
            exc_same[idx] = e
        finally:
            svc_thread._store.close()

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(worker_same, 0)
        f2 = ex.submit(worker_same, 1)
        f1.result(timeout=30)
        f2.result(timeout=30)

    succeeded = [r for r in res_same if r is not None]
    assert len(succeeded) >= 1
    intent_ids = {r.payment_intent_id for r in succeeded}
    assert len(intent_ids) == 1

    for exc in exc_same:
        if exc is not None:
            assert isinstance(exc, ServiceConflict)
            svc_retry = service(db_same, payments=shared_payments)
            try:
                retry_auth = svc_retry.authorize(
                    cid, s.StartAuthorization(payment_method_id="pm_first", terms_accepted=True)
                )
                assert retry_auth.payment_intent_id == next(iter(intent_ids))
            finally:
                svc_retry._store.close()

    assert len(shared_payments.authorizations) == 1

    cs_same = SqliteTransactionStore(db_same)
    try:
        conn = cs_same._require_open()
        assert conn.execute("SELECT COUNT(*) FROM pb_cases").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM pb_pending_effects").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM pb_intake_authorization_attempts WHERE state='authorized'"
            ).fetchone()[0]
            == 1
        )
        ws = cs_same.get_workspace(ws_id)
        assert ws is not None
        assert ws.workspace.position == INTAKE["customer_position"]["statement"]
        assert ws.workspace.photo_refs == (pref,)
    finally:
        cs_same.close()

    db_diff = tmp_path / "test_c_diff.db"
    shared_payments_diff = LockFakePaymentClient()
    svc_setup_diff = service(db_diff, payments=shared_payments_diff)

    try:
        ws_v_diff = svc_setup_diff.create_workspace("owner_c2", s.CreateWorkspace(notes="c2"))
        ws_id_diff = ws_v_diff.workspace_id
        p_ref_diff = svc_setup_diff.upload_photo(
            ws_id_diff, s.PhotoUpload(content_type="image/png", data_base64=PNG_B64)
        ).photo_ref
        body_diff = copy.deepcopy(INTAKE)
        body_diff["photo_refs"] = [p_ref_diff]
        rcpt_diff = svc_setup_diff.submit(ws_id_diff, s.SubmitIntake.model_validate(body_diff))
        cid_diff = rcpt_diff.case_id
        svc_setup_diff.choose(cid_diff, s.ChooseTier(tier=s.MailTier.CERTIFIED))
    finally:
        svc_setup_diff._store.close()

    barrier_diff = threading.Barrier(2)
    res_diff: list[s.Authorization | None] = [None, None]
    exc_diff: list[Exception | None] = [None, None]

    def worker_diff(idx: int, pm: str) -> None:
        svc_thread = service(db_diff, payments=shared_payments_diff)
        try:
            barrier_diff.wait(timeout=10)
            res_diff[idx] = svc_thread.authorize(
                cid_diff, s.StartAuthorization(payment_method_id=pm, terms_accepted=True)
            )
        except Exception as e:
            exc_diff[idx] = e
        finally:
            svc_thread._store.close()

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(worker_diff, 0, "pm_first")
        f2 = ex.submit(worker_diff, 1, "pm_second")
        f1.result(timeout=30)
        f2.result(timeout=30)

    succeeded_diff = [r for r in res_diff if r is not None]
    assert len(succeeded_diff) >= 1
    intent_ids_diff = {r.payment_intent_id for r in succeeded_diff}
    assert len(intent_ids_diff) == 1

    for exc in exc_diff:
        if exc is not None:
            assert isinstance(exc, ServiceConflict)

    assert len(shared_payments_diff.authorizations) == 1

    cs_diff = SqliteTransactionStore(db_diff)
    try:
        conn = cs_diff._require_open()
        assert conn.execute("SELECT COUNT(*) FROM pb_cases").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM pb_pending_effects").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM pb_intake_authorization_attempts WHERE state='authorized'"
            ).fetchone()[0]
            == 1
        )
        ws_d = cs_diff.get_workspace(ws_id_diff)
        assert ws_d is not None
        assert ws_d.workspace.position == INTAKE["customer_position"]["statement"]
        assert ws_d.workspace.photo_refs == (p_ref_diff,)
    finally:
        cs_diff.close()


def test_d_service_level_faults(tmp_path: Path) -> None:
    db_d1 = tmp_path / "test_d1.db"
    svc_d1 = service(db_d1)
    proposed_token = "ws_00000000000000000000000000000000"

    hooked_d1 = HookedConnection(svc_d1._store._require_open())
    svc_d1._store._database = hooked_d1  # type: ignore[assignment]
    hooked_d1.raise_on_statement = ("INSERT INTO pb_intake_owners", RuntimeError("owner fail"))

    try:
        with pytest.raises(RuntimeError):
            svc_d1.create_workspace("owner_d1", s.CreateWorkspace(notes="d1"))
    finally:
        svc_d1._store.close()

    reopened_d1 = SqliteTransactionStore(db_d1)
    try:
        conn = reopened_d1._require_open()
        assert conn.execute("SELECT COUNT(*) FROM pb_workspaces").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM pb_intake_owners").fetchone()[0] == 0
        svc_reopened_d1 = service(db_d1)
        try:
            assert svc_reopened_d1.workspace_owner(proposed_token) is None
            assert svc_reopened_d1.case_owner("case_unknown") is None
            with pytest.raises(ServiceConflict) as exc_info:
                svc_reopened_d1.workspace_view(proposed_token)
            assert str(exc_info.value) == "not_found"
        finally:
            svc_reopened_d1._store.close()
    finally:
        reopened_d1.close()

    db_d2 = tmp_path / "test_d2.db"
    svc_d2 = service(db_d2)

    try:
        ws_view = svc_d2.create_workspace("owner_d2", s.CreateWorkspace(notes="d2"))
        ws_id = ws_view.workspace_id
        photo_ref = svc_d2.upload_photo(
            ws_id, s.PhotoUpload(content_type="image/png", data_base64=PNG_B64)
        ).photo_ref

        hooked_d2 = HookedConnection(svc_d2._store._require_open())
        svc_d2._store._database = hooked_d2  # type: ignore[assignment]
        hooked_d2.raise_after_commit = RuntimeError("lost reply after submit commit")

        intake_body = copy.deepcopy(INTAKE)
        intake_body["photo_refs"] = [photo_ref]
        submit_input = s.SubmitIntake.model_validate(intake_body)

        with pytest.raises(RuntimeError):
            svc_d2.submit(ws_id, submit_input)

        intake_before = svc_d2._store.intake_for_workspace(ws_id)
        assert intake_before is not None
        recorded_intake_id = intake_before.intake_id
    finally:
        svc_d2._store.close()

    new_store = SqliteTransactionStore(db_d2)
    svc_retry = CaseService(replace(svc_d2._deps, store=new_store))
    try:
        receipt = svc_retry.submit(ws_id, submit_input)
        assert receipt.case_id == recorded_intake_id
        ws = new_store.get_workspace(ws_id)
        assert ws is not None
        assert ws.workspace.position == INTAKE["customer_position"]["statement"]
        assert ws.workspace.photo_refs == (photo_ref,)
    finally:
        new_store.close()
