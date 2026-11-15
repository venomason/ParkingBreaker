"""T20: signed ingress contracts and durable early-letter recovery."""

import hashlib
import hmac
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient

from mail_samples import make_request, seed_approval, seed_destination
from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.api.app import create_app
from parkingbreaker.appeal.adoption import text_sha256
from parkingbreaker.appeal.packet import build_packet
from parkingbreaker.approval_manifest import build_approval_manifest
from parkingbreaker.case_service import CaseService
from parkingbreaker.case_status import payment_event_for
from parkingbreaker.intake_store import SavedDraft
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import Accepted, LobResponse, Uncertain
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.research.resolution import ResearchAnswers, VehicleRelationship
from parkingbreaker.research_records import answers_json, research_json
from parkingbreaker.submission_coordinator import run_submission_step
from parkingbreaker.submission_records import SubmissionCursor
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import RevisionConflict, SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from prepared_packet_support import fetched_at
from test_api import INTAKE
from test_appeal_packet import CITATION, letter_text
from test_case_service import service
from test_submission_coordinator import NOW, step
from test_t17_pieceB_review import distinct_event
from test_tier_acceptance import extract_case, model_answers
from test_transaction_lifecycle import letter


def prepared(
    store: SqliteTransactionStore, expires_at: datetime = NOW + timedelta(days=5)
) -> CaseRecord:
    """Give handcrafted approved cases the saved timing evidence dispatch now requires."""
    # This suite crosses CaseService dispatch: use a real PDF and saved approval
    # context, rather than the coordinator-only placeholder bytes.
    store.save_workspace("workspace", Workspace(), NOW)
    store.set_workspace_owner("workspace", "synthetic-owner", NOW)
    intake_data = {**INTAKE, "ticket": {**INTAKE["ticket"], "citation_number": "SF-998877"}}
    intake = store.record_intake(
        "synthetic-intake", "workspace", json.dumps(intake_data), "{}", NOW
    )
    case = store.start_case("workspace", "auth", NOW, expires_at)
    for event in (
        cs.ResearchComplete(),
        cs.DraftReady(),
        cs.CustomerApproved(),
        cs.SubmitRequested(),
    ):
        case = letter(store, case, event)
    seed_destination(store, case, NOW)
    text = letter_text()
    packet = build_packet(text, CITATION, [], NOW, citation_number="SF-998877")
    request = make_request(pdf=packet.pdf)
    store.prepare(case.case_token, 1, request, NOW)
    store.record_approval_manifest(
        build_approval_manifest(
            case_id=intake.intake_id,
            case_token=case.case_token,
            attempt=1,
            approved_at=NOW,
            build_commit=None,
            raw_customer_text=text,
            approved_text=text,
            request=request,
            affirmations=(),
        )
    )
    store.save_draft(
        intake.intake_id,
        SavedDraft(
            text, "[]", "[]", text_sha256(text), NOW, "synthetic-owner", packet.sha256, NOW, 0
        ),
        None,
    )
    research, _, _ = extract_case(model_answers())
    research = replace(
        fetched_at(research, NOW), ticket=replace(research.ticket, issue_date=NOW.date())
    )
    answers = ResearchAnswers(
        vehicle_relationship=VehicleRelationship.REGISTERED_OWNER, notice_issue_date=NOW.date()
    )
    resolution = research.resolve(answers)
    assert resolution.ready
    assert resolution.deadline_feasible(NOW.date(), make_request().options.mail_class)
    store.save_research(intake.intake_id, research_json(research), answers_json(answers), NOW, None)
    return case


def app_for(path: Path, payments: FakePaymentClient | None = None) -> CaseService:
    app = service(path, payments or FakePaymentClient(), clock=lambda: NOW)
    object.__setattr__(app._deps, "lob_webhook_secret", "secret")
    object.__setattr__(app._deps, "new_id", lambda: uuid4().hex)
    return app


def signed(data: dict[str, object]) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(data).encode()
    timestamp = str(int(NOW.timestamp()))
    return body, {
        "lob-signature-timestamp": timestamp,
        "lob-signature": hmac.new(
            b"secret", timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest(),
    }


def pending(path: Path) -> str:
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        step(store, FakeLobClient([LobResponse(200, "ltr_early")]), case)
        return case.case_token


def letter_body() -> dict[str, object]:
    from mail_samples import make_request

    return {
        "id": "ltr_early",
        "metadata": {"submission_token": make_request().submission_token},
        "tracking_number": "940000001",
    }


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("defect", ["id", "event_type", "body", "date_created"])
async def test_signed_invalid_shape_is_409(tmp_path: Path, defect: str) -> None:
    path = tmp_path / "cases.sqlite"
    pending(path)
    data: dict[str, object] = {
        "id": "evt_shape",
        "event_type": "letter.in_transit",
        "body": letter_body(),
        "date_created": NOW.isoformat(),
    }
    data[defect] = {"id": ""}.get(defect, 123) if defect != "date_created" else "2026-10-11"
    payload, headers = signed(data)
    real = app_for(path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(real)), base_url="http://test"
        ) as client:
            response = await client.post("/webhooks/lob", content=payload, headers=headers)
        assert response.status_code == 409
        assert response.json() == {"detail": "invalid_webhook"}
        assert real._store.tracking_payloads(real._store.case_ids()[0]) == ()
    finally:
        real._store.close()


@pytest.mark.parametrize("before_case", [False, True])
def test_early_scan_survives_restart_and_cursor_landing_once(
    tmp_path: Path, before_case: bool
) -> None:
    path = tmp_path / "cases.sqlite"
    if not before_case:
        with SqliteTransactionStore(path) as store:
            case = prepared(store)
    body = letter_body()
    body["to"] = {"name": "PRIVATE-EARLY-NAME"}
    payload, headers = distinct_event(body, "letter.in_transit", NOW, "evt_early")
    real = app_for(path)
    try:
        assert real.webhook("lob", payload, headers)
        assert not real.webhook("lob", payload, headers)
        assert "PRIVATE-EARLY-NAME" not in "\n".join(real._store._require_open().iterdump())
    finally:
        real._store.close()
    with SqliteTransactionStore(path) as store:
        if before_case:
            case = prepared(store)
        assert store.submission_cursor(case.case_token, 1) is None
        assert store.get_case(case.case_token) == case
    payments = FakePaymentClient()
    lob = FakeLobClient([LobResponse(200, "ltr_early")])
    real = app_for(path, payments)
    object.__setattr__(real._deps, "lob", lob)
    try:
        real._dispatch(case.case_token)
        real._dispatch(case.case_token)
        real._dispatch(case.case_token)
        assert not real.webhook("lob", payload, headers)
        saved_cursor = real._store.submission_cursor(case.case_token, 1)
        assert saved_cursor is not None
        assert isinstance(saved_cursor.outcome, Accepted)
        current = real._store.get_case(case.case_token)
        assert current is not None
        assert current.letter_state is cs.CaseState.IN_TRANSIT
        assert current.payment_state is ps.PaymentState.CAPTURED
        assert len(real._store.tracking_payloads(case.case_token)) == 1
        assert payments.captures == 1
        assert payments.releases == 0
        assert len(lob.calls) == 1
        assert (
            len(
                [
                    row
                    for row in real._store.transition_history(case.case_token)
                    if row.event_name == "SubmitConfirmed"
                ]
            )
            == 1
        )
    finally:
        real._store.close()


@pytest.mark.parametrize("prefix", ["", "certified."])
def test_early_return_applies_only_after_acceptance_once(tmp_path: Path, prefix: str) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
    payload, headers = distinct_event(
        letter_body(), f"letter.{prefix}returned_to_sender", NOW, "evt_return"
    )
    real = app_for(path)
    try:
        assert real.webhook("lob", payload, headers)
        assert real._store.get_case(case.case_token) == case
    finally:
        real._store.close()
    payments = FakePaymentClient()
    real = app_for(path, payments)
    lob = FakeLobClient([LobResponse(200, "ltr_early")])
    object.__setattr__(real._deps, "lob", lob)
    try:
        real._dispatch(case.case_token)
        real._dispatch(case.case_token)
        real._dispatch(case.case_token)
        assert not real.webhook("lob", payload, headers)
        current = real._store.get_case(case.case_token)
        assert current is not None
        assert current.letter_state is cs.CaseState.RETURNED
        events = [row.event_name for row in real._store.transition_history(case.case_token)]
        assert events.count("ReturnConfirmed") == 1
        assert events.index("SubmitConfirmed") < events.index("ReturnConfirmed")
        assert "DeliveryProofStored" not in events
        assert payments.captures == 1
        assert len(lob.calls) == 1
    finally:
        real._store.close()


def test_token_matches_unanchored_uncertain_cursor_without_new_create(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        cursor = step(store, FakeLobClient([LobResponse(None)]), case)
        assert isinstance(cursor.outcome, Uncertain)
        assert cursor.outcome.letter_id is None
    payments = FakePaymentClient()
    real = app_for(path, payments)
    lob = FakeLobClient([])
    object.__setattr__(real._deps, "lob", lob)
    payload, headers = distinct_event(letter_body(), "letter.in_transit", NOW, "evt_token")
    try:
        assert real.webhook("lob", payload, headers)
        real._dispatch(case.case_token)
        real._dispatch(case.case_token)
        assert not real.webhook("lob", payload, headers)
        saved_cursor = real._store.submission_cursor(case.case_token, 1)
        assert saved_cursor is not None
        assert isinstance(saved_cursor.outcome, Accepted)
        assert saved_cursor.outcome.letter_id == "ltr_early"
        assert payments.captures == 1
        assert not lob.calls
    finally:
        real._store.close()


def test_metadata_free_early_return_waits_for_exact_accepted_id(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
    body = letter_body()
    del body["metadata"]
    payload, headers = distinct_event(body, "letter.returned_to_sender", NOW, "evt_no_metadata")
    real = app_for(path)
    try:
        assert real.webhook("lob", payload, headers)
    finally:
        real._store.close()
    with SqliteTransactionStore(path) as store:
        step(store, FakeLobClient([LobResponse(200, "ltr_early")]), case)
    real = app_for(path)
    try:
        real._replay_webhooks()
        current = real._store.get_case(case.case_token)
        assert current is not None
        assert current.letter_state is cs.CaseState.SUBMITTING
        assert current.payment_state is ps.PaymentState.AUTHORIZED
        # Missing token cannot supply first-scan acceptance. Once an independently
        # proven acceptance lands, replay still needs to apply the return.
        scan_body, scan_headers = distinct_event(
            letter_body(), "letter.in_transit", NOW, "evt_scan"
        )
        assert real.webhook("lob", scan_body, scan_headers)
        real._replay_webhooks()
        current = real._store.get_case(case.case_token)
        assert current is not None
        assert current.letter_state is cs.CaseState.RETURNED
        assert not real.webhook("lob", payload, headers)
    finally:
        real._store.close()


def test_unanchored_conflicting_letters_keep_hold_and_backlog(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
    real = app_for(path)
    try:
        for number in (1, 2):
            body = letter_body()
            body["id"] = f"ltr_conflict_{number}"
            payload, headers = distinct_event(body, "letter.in_transit", NOW, f"evt_{number}")
            assert real.webhook("lob", payload, headers)
    finally:
        real._store.close()
    with SqliteTransactionStore(path) as store:
        step(store, FakeLobClient([LobResponse(None)]), case)
    real = app_for(path)
    try:
        real._replay_webhooks()
        assert real._store.get_case(case.case_token) == case
        assert len(real._store.pending_webhooks()) == 2
        assert real._store.tracking_payloads(case.case_token) == ()
    finally:
        real._store.close()


def test_store_unanchored_acceptance_requires_frozen_token(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        step(store, FakeLobClient([LobResponse(None)]), case)
        with (
            store.submission_ownership(),
            pytest.raises(ValueError, match="exact pending original letter"),
        ):
            store.record_webhook(
                "lob",
                "evt_unproven",
                case.case_token,
                "{}",
                occurred_at=NOW,
                new_effect_id=lambda: uuid4().hex,
                submission_acceptance=Accepted("ltr_unproven"),
            )
        assert store.get_case(case.case_token) == case
        assert store.tracking_payloads(case.case_token) == ()


def test_early_event_identity_cannot_be_replaced_on_retry(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    real = app_for(path)
    first, first_headers = distinct_event(letter_body(), "letter.in_transit", NOW, "evt_pinned")
    try:
        assert real.webhook("lob", first, first_headers)
    finally:
        real._store.close()
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        step(store, FakeLobClient([LobResponse(200, "ltr_early")]), case)
    real = app_for(path)
    changed = letter_body()
    changed["id"] = "ltr_different"
    retry, headers = distinct_event(changed, "letter.returned_to_sender", NOW, "evt_pinned")
    try:
        assert real.webhook("lob", retry, headers)
        assert not real.webhook("lob", first, first_headers)
        current = real._store.get_case(case.case_token)
        assert current is not None
        assert current.letter_state is cs.CaseState.IN_TRANSIT
        saved = json.loads(real._store.tracking_payloads(case.case_token)[0])
        assert saved["letter_id"] == "ltr_early"
        assert saved["event"] == "letter.in_transit"
    finally:
        real._store.close()


def test_early_malformed_metadata_never_stores_nested_customer_data(tmp_path: Path) -> None:
    real = app_for(tmp_path / "cases.sqlite")
    body = letter_body()
    body["metadata"] = {"submission_token": {"customer": "PRIVATE-NESTED-METADATA"}}
    payload, headers = distinct_event(body, "letter.in_transit", NOW, "evt_bad_metadata")
    try:
        assert real.webhook("lob", payload, headers)
        assert "PRIVATE-NESTED-METADATA" not in "\n".join(real._store._require_open().iterdump())
    finally:
        real._store.close()


@pytest.mark.parametrize("crash_at", ["acceptance", "return"])
def test_replay_failure_preserves_backlog_and_committed_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_at: str
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
    real = app_for(path)
    payload, headers = distinct_event(letter_body(), "letter.returned_to_sender", NOW, "evt_crash")
    assert real.webhook("lob", payload, headers)
    object.__setattr__(real._deps, "lob", FakeLobClient([LobResponse(200, "ltr_early")]))
    original_finish = real._store.finish_submission
    original_apply = real._store._apply_event
    original_record = real._store.record_webhook
    boundaries: list[bool] = []

    def record(*args: object, **kwargs: object) -> bool:
        boundaries.append(real._store._require_open().in_transaction)
        return original_record(*args, **kwargs)  # type: ignore[arg-type]

    def finish(*args: object, **kwargs: object) -> object:
        result = original_finish(*args, **kwargs)  # type: ignore[arg-type]
        if crash_at == "acceptance" and kwargs.get("manage_transaction") is False:
            raise RuntimeError("crash after acceptance")
        return result

    def apply(*args: object, **kwargs: object) -> object:
        result = original_apply(*args, **kwargs)  # type: ignore[arg-type]
        if crash_at == "return" and isinstance(args[1], cs.ReturnConfirmed):
            raise RuntimeError("crash after return")
        return result

    monkeypatch.setattr(real._store, "record_webhook", record)
    monkeypatch.setattr(real._store, "finish_submission", finish)
    monkeypatch.setattr(real._store, "_apply_event", apply)
    try:
        with pytest.raises(RuntimeError, match="crash after"):
            real._dispatch(case.case_token)
        assert boundaries
        assert not any(boundaries)
        assert not real._store._require_open().in_transaction
        assert real._store.get_case(case.case_token) == case
        cursor = real._store.submission_cursor(case.case_token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Uncertain)
        assert cursor.outcome.letter_id == "ltr_early"  # create commit survived
        assert real._store.tracking_payloads(case.case_token) == ()
        assert len(real._store.pending_webhooks()) == 1
    finally:
        real._store.close()
    real = app_for(path)
    try:
        real._replay_webhooks()
        real._replay_webhooks()
        assert real._store.pending_webhooks() == ()
        assert len(real._store.tracking_payloads(case.case_token)) == 1
        current = real._store.get_case(case.case_token)
        assert current is not None
        assert current.letter_state is cs.CaseState.RETURNED
        assert current.payment_state is ps.PaymentState.CAPTURE_PENDING
        assert (
            len(
                [
                    e
                    for e in real._store.effects(case.case_token)
                    if e.machine == "payment" and e.effect is ps.PaymentEffect.CAPTURE_PAYMENT
                ]
            )
            == 1
        )
    finally:
        real._store.close()


def test_backlog_migration_preserves_bound_history_and_refuses_unversioned_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cases.sqlite"
    token = pending(path)
    real = app_for(path)
    payload, headers = distinct_event(letter_body(), "letter.in_transit", NOW, "evt_history")
    try:
        assert real.webhook("lob", payload, headers)
        history = real._store.tracking_payloads(token)
        db = real._store._require_open()
        db.execute("DROP TABLE pb_webhook_backlog")
        db.execute("DELETE FROM pb_store_versions WHERE component='webhook_backlog'")
    finally:
        real._store.close()
    with SqliteTransactionStore(path) as store:
        assert store.tracking_payloads(token) == history
        assert store.pending_webhooks() == ()
        store._require_open().execute(
            "DELETE FROM pb_store_versions WHERE component='webhook_backlog'"
        )
    with (
        pytest.raises(StorageCorruption, match="unversioned webhook backlog"),
        SqliteTransactionStore(path),
    ):
        pass


@pytest.mark.anyio
async def test_revision_conflict_is_retryable_without_dedup_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cases.sqlite"
    token = pending(path)
    real = app_for(path)
    original = real._store.record_webhook

    def busy(*_args: object, **_kwargs: object) -> bool:
        raise RevisionConflict("injected")

    payload, headers = distinct_event(letter_body(), "letter.in_transit", NOW, "evt_busy")
    try:
        monkeypatch.setattr(real._store, "record_webhook", busy)
        async with AsyncClient(
            transport=ASGITransport(app=create_app(real)), base_url="http://test"
        ) as client:
            response = await client.post("/webhooks/lob", content=payload, headers=headers)
            assert response.status_code == 409
            assert response.json() == {"detail": "webhook_busy"}
            assert real._store.tracking_payloads(token) == ()
            monkeypatch.setattr(real._store, "record_webhook", original)
            response = await client.post("/webhooks/lob", content=payload, headers=headers)
            assert response.status_code == 200
            assert response.json() == {"received": True, "duplicate": False}
    finally:
        real._store.close()


@pytest.mark.anyio
async def test_webhook_logs_and_storage_exclude_customer_payload(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "cases.sqlite"
    token = pending(path)
    real = app_for(path)
    body = letter_body()
    body.update({"to": {"name": "PRIVATE-CUSTOMER", "address_line1": "PRIVATE-ADDRESS"}})
    body["description"] = "PRIVATE-REASON"
    payload, headers = distinct_event(body, "letter.in_transit", NOW, "evt_private")
    caplog.set_level(logging.DEBUG)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(real)), base_url="http://test"
        ) as client:
            assert (
                await client.post("/webhooks/lob", content=payload, headers=headers)
            ).status_code == 200
        stored = real._store.tracking_payloads(token)
        assert len(stored) == 1
        assert set(json.loads(stored[0])) == {
            "event",
            "occurred_at",
            "tracking_number",
            "letter_id",
            "attempt",
            "failure_evidence",
            "scan_evidence",
            "evidence_ignored",
            "tracking_events",
        }
        for private in ("PRIVATE-CUSTOMER", "PRIVATE-ADDRESS", "PRIVATE-REASON"):
            assert private not in caplog.text
            assert private not in stored[0]
    finally:
        real._store.close()


def numbered_case(store: SqliteTransactionStore, number: int) -> CaseRecord:
    """One more frozen case with its own submission token (tok-<number>) and effect ids."""
    workspace = f"workspace-{number}"
    store.save_workspace(workspace, Workspace(), NOW)
    case = store.start_case(workspace, f"auth-{number}", NOW, NOW + timedelta(days=5))
    for event in (
        cs.ResearchComplete(),
        cs.DraftReady(),
        cs.CustomerApproved(),
        cs.SubmitRequested(),
    ):
        transition = cs.transition(case.letter_state, event, case.attempt)
        count = len(transition.effects)
        payment = case.payment_state
        for effect in transition.effects:
            mapped = payment_event_for(effect)
            if mapped is not None:
                step_out = ps.payment_transition(payment, mapped)
                count += len(step_out.effects)
                payment = step_out.target
        case = store.apply_letter_event(
            case.case_token,
            event,
            case.revision,
            occurred_at=NOW,
            effect_ids=tuple(uuid4().hex for _ in range(count)),
        )
    seed_destination(store, case, NOW)
    store.prepare(case.case_token, 1, make_request(token=f"tok-{number}"), NOW)
    seed_approval(store, case.case_token)
    return case


def land_cursor(
    store: SqliteTransactionStore, case: CaseRecord, response: LobResponse
) -> SubmissionCursor:
    """Run the first create for a case so its submission cursor exists."""
    return run_submission_step(
        store,
        FakeLobClient([response]),
        case.case_token,
        case.attempt,
        expected_case_revision=case.revision,
        expected_cursor_revision=None,
        operation_id=uuid4().hex,
        clock=lambda: NOW,
        new_effect_id=lambda: uuid4().hex,
    )


def capture_effect_count(store: SqliteTransactionStore, case: CaseRecord) -> int:
    return sum(
        1
        for effect in store.effects(case.case_token)
        if effect.machine == "payment" and effect.effect is ps.PaymentEffect.CAPTURE_PAYMENT
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("deleted", True),
        ("deleted", "yes"),
        ("status", "failed"),
        ("failure_reason", "address_undeliverable"),
        ("failure_reason", {"code": "PRIVATE-FAILURE-TEXT"}),
    ],
)
def test_backlogged_scan_for_a_deleted_or_failed_letter_never_accepts(
    tmp_path: Path, field: str, value: object
) -> None:
    """The reduced envelope keeps every predicate field, so replay cannot accept it (M1)."""
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
    body = letter_body()
    body[field] = value
    payload, headers = distinct_event(body, "letter.in_transit", NOW, "evt_unsafe_scan")
    real = app_for(path)
    try:
        assert real.webhook("lob", payload, headers)
        assert "PRIVATE-FAILURE-TEXT" not in "\n".join(real._store._require_open().iterdump())
    finally:
        real._store.close()
    with SqliteTransactionStore(path) as store:
        landed = land_cursor(store, case, LobResponse(200, "ltr_early"))
        assert isinstance(landed.outcome, Uncertain)
        assert landed.outcome.letter_id == "ltr_early"
    payments = FakePaymentClient()
    real = app_for(path, payments)
    try:
        real._replay_webhooks()
        cursor = real._store.submission_cursor(case.case_token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Uncertain)
        current = real._store.get_case(case.case_token)
        assert current is not None
        assert current.letter_state is cs.CaseState.SUBMITTING
        assert current.payment_state is ps.PaymentState.AUTHORIZED
        assert capture_effect_count(real._store, case) == 0
        assert payments.captures == 0
    finally:
        real._store.close()


@pytest.mark.parametrize("anchored_case_first", [True, False])
def test_letter_id_of_one_case_with_token_of_another_stays_parked(
    tmp_path: Path, anchored_case_first: bool
) -> None:
    """An event claiming two cases is ambiguous; it is never taken by first match (M1)."""
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        cases = [numbered_case(store, 1), numbered_case(store, 2)]
        anchored, unanchored = cases if anchored_case_first else cases[::-1]
        first = land_cursor(store, anchored, LobResponse(200, "ltr_anchored"))
        second = land_cursor(store, unanchored, LobResponse(None))
        assert isinstance(first.outcome, Uncertain)
        assert first.outcome.letter_id == "ltr_anchored"
        assert isinstance(second.outcome, Uncertain)
        assert second.outcome.letter_id is None
        token_of_unanchored = f"tok-{1 if unanchored is cases[0] else 2}"
    body: dict[str, object] = {
        "id": "ltr_anchored",
        "metadata": {"submission_token": token_of_unanchored},
    }
    payload, headers = distinct_event(body, "letter.in_transit", NOW, "evt_split_identity")
    real = app_for(path)
    try:
        assert real.webhook("lob", payload, headers)
        real._replay_webhooks()
        real._replay_webhooks()
        assert [row[1] for row in real._store.pending_webhooks()] == ["evt_split_identity"]
        for case in cases:
            assert real._store.get_case(case.case_token) == case
            assert real._store.tracking_payloads(case.case_token) == ()
            assert capture_effect_count(real._store, case) == 0
        left = real._store.submission_cursor(unanchored.case_token, 1)
        assert left is not None
        assert isinstance(left.outcome, Uncertain)
        assert left.outcome.letter_id is None
    finally:
        real._store.close()
