"""Ordinary-post feedback stays tied to carrier evidence and customer statements."""

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient

from mail_samples import make_request
from parkingbreaker import case_state as cs
from parkingbreaker.api.app import create_app
from parkingbreaker.case_service import CaseService
from parkingbreaker.email_client import EmailOutcome, EmailReason, FakeEmailClient
from parkingbreaker.lob_submission import Accepted, SubmissionOutcome, Uncertain, UspsScan
from parkingbreaker.mail_config import MailClass
from parkingbreaker.outcome_follow_up import OutcomeLinkService, derive_follow_up_token
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_beta_acceptance_api import ALICE, BOB, Clock, build_service, counts, selected
from test_beta_admission import NOW
from test_beta_feedback_api import notice_body, upload
from test_first_class_pilot import enroll, open_pilot, save_intake
from test_outcome_follow_up import LINK_KEY, _accepted, _service

CASE_PATH = "/cases/case_1/pilot-feedback"
LINK_PATH = "/outcomes/pilot-feedback"
SUBMISSION_TOKEN = "ordinary-post-fixture"
LETTER_ID = "ltr_pilot"
FIRST_SCAN = NOW + timedelta(days=2)


def select_pilot(path: Path) -> None:
    with SqliteTransactionStore(path) as store:
        enroll(store)
        open_pilot(store)
        intake = save_intake(store, 1, owner="alice")
        assert store.offer_first_class_pilot(intake, NOW) is not None


def _ready(
    store: SqliteTransactionStore, mail_class: MailClass = MailClass.FIRST_CLASS
) -> CaseRecord:
    entitlement = store.beta_entitlement("case_1")
    assert entitlement is not None
    case = store.start_beta_case("case_1", entitlement.disclosure_sha256, NOW)
    for event in (cs.ResearchComplete(), cs.DraftReady(), cs.CustomerApproved()):
        case = store.apply_letter_event(case.case_token, event, case.revision, occurred_at=NOW)
    request = make_request(token=SUBMISSION_TOKEN)
    store.prepare(
        case.case_token,
        1,
        replace(request, options=replace(request.options, mail_class=mail_class)),
        NOW,
    )
    return store.apply_letter_event(
        case.case_token,
        cs.SubmitRequested(),
        case.revision,
        occurred_at=NOW,
        new_effect_id=lambda: uuid4().hex,
    )


def _finish(store: SqliteTransactionStore, case: CaseRecord, outcome: SubmissionOutcome) -> None:
    with store.submission_ownership():
        claimed, _ = store.claim_submission(
            case.case_token, 1, case.revision, None, now=NOW, operation_id="ordinary-create"
        )
        store.finish_submission(claimed, outcome, now=FIRST_SCAN, effect_ids=())


def _scan(at: datetime | None = FIRST_SCAN) -> UspsScan:
    return UspsScan("normal", "In Transit", None if at is None else at.isoformat())


def _api(service: CaseService, clock: Clock) -> AsyncClient:
    links = OutcomeLinkService(service._store, key=LINK_KEY, clock=clock)
    return AsyncClient(
        transport=ASGITransport(app=create_app(service, outcome_links=links)),
        base_url="https://local.test",
    )


def _signed_webhook(
    event: str,
    event_id: str,
    body: Mapping[str, object],
    received_at: datetime,
    occurred_at: datetime | None = None,
) -> tuple[bytes, dict[str, str]]:
    envelope: dict[str, object] = {"id": event_id, "event_type": {"id": event}, "body": body}
    if occurred_at is not None:
        envelope["date_created"] = occurred_at.isoformat()
    payload = json.dumps(envelope).encode()
    stamp = str(int(received_at.timestamp()))
    signature = hmac.new(b"secret", stamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    return payload, {"lob-signature": signature, "lob-signature-timestamp": stamp}


@pytest.mark.anyio
async def test_private_pilot_reports_and_revision_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    select_pilot(path)
    clock = Clock(FIRST_SCAN)
    service = build_service(path, clock=clock)
    store = service._store
    object.__setattr__(service._deps, "lob_webhook_secret", "secret")
    try:
        case = _ready(store)
        token = case.case_token
        async with _api(service, clock) as api:
            assert (await api.get(CASE_PATH, headers=ALICE)).json()["eligible"] is False
            early = await api.post(
                CASE_PATH,
                headers=ALICE,
                json={"request_id": "early", "expected_revision": 0, "routing": "returned"},
            )
            assert early.status_code == 409
            assert early.json() == {"detail": "pilot_feedback_not_eligible"}
            _finish(store, case, Uncertain("rendered", LETTER_ID, rendered=True))
            body = {
                "id": LETTER_ID,
                "metadata": {"submission_token": SUBMISSION_TOKEN},
                "status": "rendered",
                "tracking_events": [
                    _scan(FIRST_SCAN + timedelta(hours=1)).as_sent(),
                    _scan().as_sent(),
                ],
            }
            payload, headers = _signed_webhook("letter.in_transit", "evt_scan", body, clock())
            observed = await api.post("/webhooks/lob", content=payload, headers=headers)
            assert observed.status_code == 200, observed.text
            assert (await api.get(CASE_PATH, headers=BOB)).status_code == 404
            snapshot = await api.get(CASE_PATH, headers=ALICE)
            assert snapshot.headers["cache-control"] == "no-store"
            assert snapshot.json() == {
                "eligible": True,
                "revision": 0,
                "first_postal_scan_at": FIRST_SCAN.isoformat().replace("+00:00", "Z"),
                "printer_handoff_at": None,
                "routing": None,
                "acknowledgment": None,
                "disposition": None,
                "updated_at": None,
                "source": "customer_reported",
            }

            before = (store.get_case(token), store.transition_history(token), counts(store))
            pending = {
                "request_id": "first-report",
                "expected_revision": 0,
                "routing": "unknown",
                "acknowledgment": "pending",
                "disposition": "pending",
            }
            report = await api.post(CASE_PATH, headers=ALICE, json=pending)
            assert report.status_code == 200, report.text
            assert report.json()["acknowledgment"] == "pending"
            assert (await api.post(CASE_PATH, headers=ALICE, json=pending)).json() == report.json()
            changed = await api.post(
                CASE_PATH, headers=ALICE, json={**pending, "routing": "returned"}
            )
            assert changed.status_code == 409
            empty = await api.post(
                CASE_PATH, headers=ALICE, json={"request_id": "empty", "expected_revision": 1}
            )
            assert empty.status_code == 422
            invented = await api.post(
                CASE_PATH,
                headers=ALICE,
                json={"request_id": "invalid", "expected_revision": 1, "routing": "delivered"},
            )
            assert invented.status_code == 422

            email = FakeEmailClient([_accepted()])
            clock.now += timedelta(days=14)
            assert _service(service, email, clock).run().sent == 1
            msg = email._messages[f"pb-pilot-followup-D14-{token}"]
            fragment = parse_qs(msg.text.split("/outcomes/pilot#", 1)[1].splitlines()[0])
            credentials = {"case_id": fragment["case"][0], "token": fragment["token"][0]}
            opt_out = {"case_id": token, "token": fragment["opt_out_token"][0]}
            assert opt_out["token"] == derive_follow_up_token(LINK_KEY, token)
            assert opt_out["token"] != credentials["token"]
            assert "First-Class Mail" in msg.text
            assert "Certified" not in msg.text
            assert (await api.post(f"{LINK_PATH}/status", json=opt_out)).status_code == 400
            assert (await api.post("/outcomes/opt-out", json=credentials)).status_code == 400
            assert (await api.post(f"{LINK_PATH}/status", json=credentials)).json() == report.json()
            final = await api.post(
                LINK_PATH,
                json={
                    **credentials,
                    "request_id": "later-report",
                    "expected_revision": 1,
                    "disposition": "reduced",
                },
            )
            assert final.status_code == 200, final.text
            assert final.json()["routing"] == "unknown"
            assert final.json()["acknowledgment"] == "pending"
            assert final.json()["disposition"] == "reduced"
            assert final.json()["revision"] == 2
            stale = await api.post(
                CASE_PATH,
                headers=ALICE,
                json={**pending, "request_id": "delayed-different-report"},
            )
            assert stale.status_code == 409
            assert stale.json() == {"detail": "pilot_feedback_revision_changed"}
            assert (await api.post(CASE_PATH, headers=ALICE, json=pending)).json() == final.json()
            assert (
                await api.post("/outcomes/report", json={**opt_out, "decision": "dismissed"})
            ).status_code == 400
            assert store.outcome_for(token) is None
            assert before == (store.get_case(token), store.transition_history(token), counts(store))
            assert store.get_case(token) is not None
            assert case.payment_state is PaymentState.NO_CHARGE

            evidence = await upload(api)
            limited = await api.get("/cases/case_1/notices")
            assert limited.status_code == 401
            notice = await api.post(
                "/cases/case_1/notices", headers=ALICE, json=notice_body([evidence])
            )
            assert notice.status_code == 201, notice.text
            assert notice.json()["review_state"] == "unreviewed"
    finally:
        store.close()

    reopened = build_service(path, clock=clock)
    try:
        assert reopened.pilot_feedback("case_1").model_dump(mode="json") == final.json()
        clock.now += timedelta(days=60)
        report_after_decision = _service(reopened, email, clock).run()
        assert report_after_decision.sent == report_after_decision.unknown_recorded == 0
        assert email.call_count == 1
    finally:
        reopened._store.close()


@pytest.mark.parametrize(
    ("outcome", "eligible"),
    [
        (Uncertain("unanchored"), False),
        (Uncertain("created", LETTER_ID), True),
        (Uncertain("rendered", LETTER_ID, rendered=True), True),
        (Accepted(LETTER_ID), True),
        (Accepted(LETTER_ID, scans=(UspsScan("normal", "Mailed", FIRST_SCAN.isoformat()),)), True),
        (Accepted(LETTER_ID, scans=(_scan(None),)), True),
    ],
)
def test_undated_or_nonpostal_events_cannot_schedule_a_reminder(
    tmp_path: Path, outcome: SubmissionOutcome, eligible: bool
) -> None:
    path = tmp_path / "pb.sqlite"
    select_pilot(path)
    clock = Clock(FIRST_SCAN + timedelta(days=31))
    service = build_service(path, clock=clock)
    with service._store as store:
        case = _ready(store)
        _finish(store, case, outcome)
        snapshot = store.pilot_feedback(case.case_token)
        assert snapshot.eligible is eligible
        assert snapshot.first_postal_scan_at is None
        if eligible:
            reported = store.record_pilot_feedback(
                case.case_token, "own-report", 0, {"routing": "returned"}, now=clock()
            )
            assert reported.routing == "returned"
            assert store.outcome_for(case.case_token) is None
        if isinstance(outcome, Accepted):
            current = store.get_case(case.case_token)
            assert current is not None
            store.apply_letter_event(
                case.case_token,
                cs.DeliveryProofStored("fixture-proof"),
                current.revision,
                occurred_at=FIRST_SCAN,
            )
        email = FakeEmailClient()
        assert _service(service, email, clock).run().sent == 0
        assert email.call_count == 0


def test_partial_updates_keep_d30_and_opt_out_is_respected(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    select_pilot(path)
    clock = Clock(FIRST_SCAN + timedelta(days=13, hours=23))
    service = build_service(path, clock=clock)
    with service._store as store:
        case = _ready(store)
        _finish(store, case, Accepted(LETTER_ID, scans=(_scan(),)))
        email = FakeEmailClient([_accepted(), _accepted()])
        follow_ups = _service(service, email, clock)
        assert follow_ups.run().sent == email.call_count == 0
        clock.now += timedelta(hours=1)
        assert follow_ups.run().sent == 1
        saved = store.record_pilot_feedback(
            case.case_token, "partial", 0, {"routing": "wrong_office"}, now=clock()
        )
        assert saved.acknowledgment is saved.disposition is None
        links = OutcomeLinkService(store, key=LINK_KEY, clock=clock)
        assert links.opt_out(case.case_token, derive_follow_up_token(LINK_KEY, case.case_token))
        clock.now += timedelta(days=16)
        assert follow_ups.run().skipped_no_consent == 1
        assert email.call_count == 1
        store.set_follow_up_consent(
            "alex@example.test", True, "resume-reminders", "customer", now=clock()
        )
        assert follow_ups.run().sent == 1
        assert follow_ups.run().sent == 0
        clock.now += timedelta(days=31)
        assert follow_ups.run().unknown_recorded == 0
        assert store.outcome_for(case.case_token) is None
        later = store.record_pilot_feedback(
            case.case_token, "later", 1, {"acknowledgment": "unknown"}, now=clock()
        )
        assert later.routing == "wrong_office"
        assert later.acknowledgment == "unknown"


def test_late_first_sweep_only_sends_the_current_reminder(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    select_pilot(path)
    clock = Clock(FIRST_SCAN + timedelta(days=31))
    service = build_service(path, clock=clock)
    with service._store as store:
        case = _ready(store)
        _finish(store, case, Accepted(LETTER_ID, scans=(_scan(),)))
        email = FakeEmailClient([_accepted()])
        follow_up = _service(service, email, clock)
        assert follow_up.run().sent == email.call_count == 1
        assert not store.follow_up_sent(case.case_token, "D14")
        assert store.follow_up_sent(case.case_token, "D30")
        assert case.case_token not in store.pilot_follow_up_candidates()
        assert follow_up.run().sent == 0


def test_pilot_email_cannot_retry_beyond_provider_idempotency_window(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    select_pilot(path)
    clock = Clock(FIRST_SCAN + timedelta(days=14))
    service = build_service(path, clock=clock)
    with service._store as store:
        case = _ready(store)
        _finish(store, case, Accepted(LETTER_ID, scans=(_scan(),)))
        failed = FakeEmailClient([EmailOutcome("uncertain", reason=EmailReason.TRANSPORT_ERROR)])
        assert _service(service, failed, clock).run().deferred == 1
        assert not store.follow_up_sent(case.case_token, "D14")
    clock.now += timedelta(hours=24)
    reopened = build_service(path, clock=clock)
    with reopened._store as store:
        email = FakeEmailClient([_accepted()])
        follow_up = _service(reopened, email, clock)
        assert follow_up.run().sent == email.call_count == 0
        clock.now = FIRST_SCAN + timedelta(days=30)
        assert follow_up.run().sent == email.call_count == 1
        assert not store.follow_up_sent(case.case_token, "D14")
        assert store.follow_up_sent(case.case_token, "D30")


def test_regular_post_alone_cannot_enroll_a_nonparticipant_in_follow_up(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    selected(path)
    clock = Clock(FIRST_SCAN + timedelta(days=15))
    service = build_service(path, clock=clock)
    with service._store as store:
        case = _ready(store, MailClass.FIRST_CLASS)
        _finish(store, case, Accepted(LETTER_ID, scans=(_scan(),)))
        assert not store.pilot_feedback(case.case_token).eligible
        assert case.case_token not in store.pilot_follow_up_candidates()
        email = FakeEmailClient()
        assert _service(service, email, clock).run().sent == email.call_count == 0


def test_legacy_free_certified_case_still_waits_for_delivery(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    selected(path)
    clock = Clock(FIRST_SCAN + timedelta(days=15))
    service = build_service(path, clock=clock)
    with service._store as store:
        case = _ready(store, MailClass.CERTIFIED)
        _finish(store, case, Accepted(LETTER_ID, scans=(_scan(),)))
        assert not service.pilot_feedback("case_1").eligible
        email = FakeEmailClient([_accepted()])
        assert _service(service, email, clock).run().sent == email.call_count == 0
        current = store.get_case(case.case_token)
        assert current is not None
        store.apply_letter_event(
            case.case_token,
            cs.DeliveryProofStored("legacy-proof"),
            current.revision,
            occurred_at=clock(),
        )
        clock.now += timedelta(days=14)
        assert _service(service, email, clock).run().sent == 1
        assert "Certified" in email._messages[f"pb-followup-D14-{case.case_token}"].text


def test_printer_handoff_requires_bound_original_id_token_and_signed_date(tmp_path: Path) -> None:
    path = tmp_path / "pb.sqlite"
    select_pilot(path)
    clock = Clock(FIRST_SCAN)
    service = build_service(path, clock=clock)
    object.__setattr__(service._deps, "lob_webhook_secret", "secret")
    with service._store as store:
        case = _ready(store)
        token = case.case_token
        body = {
            "id": LETTER_ID,
            "metadata": {"submission_token": SUBMISSION_TOKEN},
            "status": "rendered",
        }
        # An early undated envelope is replayed with the original synthesized receipt
        # timestamp. Replay must never promote that serialization to a provider date.
        assert service.webhook("lob", *_signed_webhook("letter.mailed", "evt_early", body, clock()))
        assert store.backlog_row("lob", "evt_early") is not None
        _finish(store, case, Uncertain("rendered", LETTER_ID, rendered=True))
        service._replay_case_webhooks(token)
        assert store.backlog_row("lob", "evt_early") is None
        assert store.pilot_feedback(token).printer_handoff_at is None

        mismatched = {**body, "metadata": {"submission_token": "other-token"}}
        assert service.webhook(
            "lob", *_signed_webhook("letter.mailed", "evt_other", mismatched, clock(), FIRST_SCAN)
        )
        assert store.pilot_feedback(token).printer_handoff_at is None

        handoff_at = FIRST_SCAN + timedelta(hours=1)
        clock.now = handoff_at + timedelta(days=1)
        assert service.webhook(
            "lob", *_signed_webhook("letter.mailed", "evt_signed", body, clock(), handoff_at)
        )
        snapshot = store.pilot_feedback(token)
        assert snapshot.printer_handoff_at == handoff_at
        assert snapshot.first_postal_scan_at is None
        email = FakeEmailClient([_accepted()])
        follow_ups = _service(service, email, clock)
        clock.now = handoff_at + timedelta(days=14) - timedelta(seconds=1)
        assert follow_ups.run().sent == 0
        clock.now += timedelta(seconds=1)
        assert follow_ups.run().sent == 1
        latest = store.get_case(token)
        assert latest is not None
        assert latest.letter_state is cs.CaseState.SUBMITTING
        assert latest.payment_state is PaymentState.NO_CHARGE
        assert store.outcome_for(token) is None
