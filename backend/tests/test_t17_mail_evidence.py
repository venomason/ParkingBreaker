"""T17 contracts through the real SDK and durable mail/payment dispatch path.

Only urllib3's socket boundary is replaced: SDK multipart encoding, response parsing,
local validation and the production coordinators/stores remain real. Payloads and case
seeds are shared with existing tests. Historical Accepted seeds below are lifecycle
fixtures, never a claim that rendering qualifies as production evidence.

T17.1/7 need current provider/account qualification, not invented offline evidence.
No undocumented production event, PDF-digest response field, plan entitlement or key
lifetime is asserted here. See provider-qualification-2026-10-17/lob-production.md.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from lob_python.api_client import ApiClient  # type: ignore[import-untyped]
from lob_python.configuration import Configuration  # type: ignore[import-untyped]
from urllib3 import HTTPResponse

from mail_samples import RECIPIENT, make_request, seed_approval, seed_destination
from parkingbreaker import case_state as cs
from parkingbreaker import payment_state as ps
from parkingbreaker.address_policy import (
    AddressProvenance,
    ConsentEvent,
    DestinationDecision,
    Disclosure,
    address_fingerprint,
)
from parkingbreaker.address_store import _json
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.lob_client import LobSdkClient
from parkingbreaker.lob_submission import Accepted, LetterOptions, LetterRequest, Uncertain
from parkingbreaker.mail_config import MailClass
from parkingbreaker.mail_request import validate_request
from parkingbreaker.payment_fakes import FakePaymentClient, full_outcome
from parkingbreaker.payment_records import PaymentOutcome
from parkingbreaker.returned_mail import (
    ReturnCause,
    UspsReturnReason,
    classify_return,
    decide_remedy,
)
from parkingbreaker.state_table import IllegalTransition
from parkingbreaker.submission_coordinator import run_submission_step
from parkingbreaker.submission_records import SubmissionCursor
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_effect_dispatch import payment_fixture
from test_lob_client import _letter, _parse_multipart
from test_stripe_payment import REF, Transport
from test_stripe_payment import client as stripe_client
from test_submission_coordinator import NOW, prepared
from test_transaction_lifecycle import letter

TOKEN = make_request().submission_token
ORIGINAL = "ltr_Original"
CORRECTED = replace(RECIPIENT, line1="987 Corrected Avenue")
Responder = Callable[[str, str], tuple[int, object]]
WireRequest = tuple[str, str, dict[str, str], bytes]


def new_id() -> str:
    return uuid4().hex


def clock_at(now: datetime) -> Callable[[], datetime]:
    return lambda: now


class OfflineLob:
    """Keep the real SDK/urllib3 encoding; replace only the final socket operation."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, respond: Responder) -> None:
        self.requests: list[WireRequest] = []
        configuration = Configuration(host="https://lob.invalid/v1", username="offline-key")
        configuration.retries = 0
        api = ApiClient(configuration)
        monkeypatch.setattr(api.rest_client.pool_manager, "urlopen", self.urlopen)
        self.client = LobSdkClient("offline-key", "2024-01-01", 1, api_client=api)
        self.respond = respond

    def urlopen(self, method: str, url: str, **kwargs: object) -> HTTPResponse:
        try:
            return self._exchange(method, url, **kwargs)
        except Exception as exc:
            # Production deliberately catches provider exceptions. A broken fixture
            # must fail the test, never masquerade as a passing uncertain response.
            pytest.fail(f"Offline Lob fixture failed for {method} {url}: {exc}")

    def _exchange(self, method: str, url: str, **kwargs: object) -> HTTPResponse:
        parsed = urlsplit(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        raw_headers = kwargs.get("headers", {})
        assert isinstance(raw_headers, Mapping)
        headers = {str(key).lower(): str(value) for key, value in raw_headers.items()}
        body = kwargs.get("body") or b""
        assert isinstance(body, bytes)
        self.requests.append((method, path, headers, body))
        status, payload = self.respond(method, path)
        return HTTPResponse(
            body=json.dumps(payload).encode(),
            status=status,
            headers={"Content-Type": "application/json"},
        )

    @property
    def creates(self) -> list[WireRequest]:
        return [request for request in self.requests if request[0] == "POST"]


def resource(**changes: object) -> dict[str, object]:
    return {**_letter(ORIGINAL, TOKEN), **changes}


def list_response(rows: list[dict[str, object]]) -> tuple[int, object]:
    return 200, {"object": "list", "data": rows, "count": len(rows), "next_url": None}


def unavailable(_method: str, _path: str) -> tuple[int, object]:
    return 503, {"error": {"code": "internal_server_error"}}


def current_case(store: SqliteTransactionStore, case: CaseRecord) -> CaseRecord:
    saved = store.get_case(case.case_token)
    assert saved is not None
    return saved


def cursor_for(store: SqliteTransactionStore, case: CaseRecord) -> SubmissionCursor:
    cursor = store.submission_cursor(case.case_token, case.attempt)
    assert cursor is not None
    return cursor


def submit(
    store: SqliteTransactionStore, wire: OfflineLob, case: CaseRecord, now: datetime
) -> SubmissionCursor:
    saved = current_case(store, case)
    cursor = store.submission_cursor(case.case_token, saved.attempt)
    return run_submission_step(
        store,
        wire.client,
        case.case_token,
        saved.attempt,
        expected_case_revision=saved.revision,
        expected_cursor_revision=None if cursor is None else cursor.revision,
        operation_id=new_id(),
        clock=lambda: now,
        new_effect_id=new_id,
    )


def assert_frozen_create(wire: OfflineLob, store: SqliteTransactionStore, case: CaseRecord) -> None:
    frozen = store.get(case.case_token, case.attempt)
    assert frozen is not None
    assert wire.creates
    for _method, path, headers, body in wire.creates:
        assert path == "/v1/letters"
        assert headers["idempotency-key"] == frozen.key
        fields = _parse_multipart(headers["content-type"], body)
        assert fields["file"] == frozen.request.pdf
        assert json.loads(fields["metadata"]) == {
            "submission_token": frozen.request.submission_token
        }


def assert_nonaccepted(
    store: SqliteTransactionStore,
    case: CaseRecord,
    payments: FakePaymentClient,
    now: datetime,
) -> None:
    assert not any(call[0] == "capture" for call in payments.calls), (
        "Capture was requested without original-letter production/mail-stream evidence"
    )
    assert not any(call[0] == "release" for call in payments.calls)
    assert current_case(store, case).payment_state is ps.PaymentState.AUTHORIZED
    assert not any(
        row.effect in {ps.PaymentEffect.CAPTURE_PAYMENT, ps.PaymentEffect.RELEASE_HOLD}
        for row in store.effects(case.case_token)
    )
    cursor = cursor_for(store, case)
    assert isinstance(cursor.outcome, Uncertain)
    assert cursor.owner is None
    assert now < cursor.next_at <= now + timedelta(minutes=30)


@pytest.mark.parametrize(
    "evidence",
    [
        "http-id-only",
        "pending",
        "rendered",
        "bare-tracking",
        "failed",
        "deleted",
        "wrong-token",
        "wrong-id-on-retrieve",
        "unknown-status",
    ],
)
def test_create_without_production_evidence_keeps_hold_and_never_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    payload = resource()
    if evidence == "http-id-only":
        payload.pop("status")
    elif evidence == "pending" or evidence == "wrong-id-on-retrieve":
        payload["status"] = "processed"
    elif evidence == "bare-tracking":
        payload["tracking_number"] = "9407111899223197428490"
    elif evidence == "failed":
        payload["status"] = "failed"
    elif evidence == "deleted":
        payload["deleted"] = True
    elif evidence == "wrong-token":
        payload["metadata"] = {"submission_token": "different-token"}
    elif evidence == "unknown-status":
        payload["status"] = "unqualified-state"

    def respond(method: str, path: str) -> tuple[int, object]:
        if method == "GET":
            assert path == f"/v1/letters/{ORIGINAL}"
            if evidence == "wrong-id-on-retrieve":
                return 200, {**payload, "id": "ltr_Different", "status": "rendered"}
        return 200, payload

    wire = OfflineLob(monkeypatch, respond)
    payments = FakePaymentClient()
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        for _ in range(2):
            dispatch_once(store, wire.client, payments, lambda: NOW, new_id)
        assert len(wire.creates) == 1  # No busy-loop or early replay.
        assert len(wire.requests) <= 2
        assert_frozen_create(wire, store, case)
        assert_nonaccepted(store, case, payments, NOW)


def prepared_request(
    store: SqliteTransactionStore, request: LetterRequest, authorization_ref: str = "auth"
) -> CaseRecord:
    """The existing low-level case fixture with a caller-selected frozen request/ref."""
    store.save_workspace("workspace", Workspace(), NOW)
    case = store.start_case("workspace", authorization_ref, NOW, NOW + timedelta(days=5))
    for event in (
        cs.ResearchComplete(),
        cs.DraftReady(),
        cs.CustomerApproved(),
        cs.SubmitRequested(),
    ):
        case = letter(store, case, event)
    seed_destination(store, case, NOW, request.recipient)
    store.prepare(case.case_token, 1, request, NOW)
    seed_approval(store, case.case_token)
    return case


def test_rendered_letter_cannot_reach_stripe_capture_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = OfflineLob(monkeypatch, lambda *_: (200, resource()))
    transport = Transport(payment_fixture(), payment_fixture("succeeded"))
    payments = stripe_client(transport)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared_request(store, make_request(), REF)
        for _ in range(2):
            dispatch_once(store, wire.client, payments, lambda: NOW, new_id)
        assert_frozen_create(wire, store, case)
        assert not any(call[1].endswith("/capture") for call in transport.calls), (
            "A rendered-only Lob response reached the real Stripe SDK capture endpoint"
        )
        assert current_case(store, case).payment_state is ps.PaymentState.AUTHORIZED


@pytest.mark.parametrize(
    "evidence",
    ["rendered", "wrong-id", "wrong-token", "different-pdf", "pending", "failed", "deleted"],
)
def test_lookup_does_not_promote_inadequate_original_letter_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    payload = resource()
    if evidence in {"wrong-id", "different-pdf"}:
        payload["id"] = "ltr_Different"
    elif evidence == "wrong-token":
        payload["metadata"] = {"submission_token": "different-token"}
    elif evidence in {"pending", "failed"}:
        payload["status"] = "processed" if evidence == "pending" else "failed"
    elif evidence == "deleted":
        payload["deleted"] = True
    other_packet = b"%PDF-1.4\na different, unapproved packet"
    posted_pdfs: list[bytes] = []

    def respond(method: str, path: str) -> tuple[int, object]:
        if method == "POST":
            request = wire.requests[-1]
            posted_pdfs.append(_parse_multipart(request[2]["content-type"], request[3])["file"])
            if evidence == "different-pdf" and posted_pdfs[-1] == other_packet:
                return 200, payload
            if evidence == "wrong-id":
                return 200, resource(status="processed")
            return unavailable(method, path)
        if "?" in path:
            query = parse_qs(urlsplit(path).query)
            assert query["metadata[submission_token]"] == [TOKEN]
            assert "metadata" not in query
            return list_response([payload])
        if evidence == "wrong-id" and len(wire.creates) == 1 and len(wire.requests) == 2:
            return 200, resource(status="processed")
        return 200, payload

    wire = OfflineLob(monkeypatch, respond)
    payments = FakePaymentClient()
    if evidence == "different-pdf":
        # A real SDK create establishes which bytes belong to the other provider ID.
        # No fictional provider PDF/hash field is used. Metadata tokens alone cannot
        # distinguish this packet from the customer's original frozen packet.
        wire.client.create_letter(make_request(other_packet), "other-packet-key")
        assert posted_pdfs == [other_packet]
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        first = submit(store, wire, case, NOW)
        assert isinstance(first.outcome, Uncertain)
    now = NOW + timedelta(hours=23)
    with SqliteTransactionStore(path) as store:
        cursor = submit(store, wire, case, now)
        for _ in range(2):
            dispatch_once(store, wire.client, payments, clock_at(now), new_id)
        assert cursor.lookup_only
        assert posted_pdfs[-1] == make_request().pdf
        assert len(wire.creates) == (2 if evidence == "different-pdf" else 1)
        assert_nonaccepted(store, case, payments, now)


@pytest.mark.parametrize("lookup", ["empty", "ambiguous", "unavailable"])
def test_ambiguous_acceptance_never_changes_key_or_resends_after_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lookup: str
) -> None:
    def respond(method: str, path: str) -> tuple[int, object]:
        if method == "POST" or lookup == "unavailable":
            return unavailable(method, path)
        if lookup == "empty":
            return list_response([])
        return list_response([resource(), resource(id="ltr_Second")])

    wire = OfflineLob(monkeypatch, respond)
    payments = FakePaymentClient()
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared(store)
        first = submit(store, wire, case, NOW)
    for now in (
        first.next_at,
        NOW + timedelta(hours=23),
        NOW + timedelta(days=1),
        NOW + timedelta(days=3),
    ):
        with SqliteTransactionStore(path) as store:
            dispatch_once(store, wire.client, payments, clock_at(now), new_id)
            assert_nonaccepted(store, case, payments, now)
            cursor = cursor_for(store, case)
            assert cursor.first_sent_at == NOW
            assert cursor.sends == 2
            assert cursor.lookup_only is (now >= NOW + timedelta(hours=23))
    with SqliteTransactionStore(path) as store:
        assert len(wire.creates) == 2
        assert_frozen_create(wire, store, case)
        assert len({request[2]["idempotency-key"] for request in wire.creates}) == 1
        store.prepare(case.case_token, 2, make_request(token="replacement-token"), now)
        with pytest.raises(ValueError, match="matching approved attempt"):
            run_submission_step(
                store,
                wire.client,
                case.case_token,
                2,
                expected_case_revision=case.revision,
                expected_cursor_revision=None,
                operation_id=new_id(),
                clock=lambda: now,
            )
        assert len(wire.creates) == 2


@pytest.mark.parametrize("invalid", ["empty-pdf", "blank-token", "bad-zip", "invalid-options"])
def test_invalid_local_request_is_refused_before_persistence_or_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    request = make_request()
    if invalid == "empty-pdf":
        request = replace(request, pdf=b"")
    elif invalid == "blank-token":
        request = replace(request, submission_token=" ")
    elif invalid == "bad-zip":
        request = replace(request, recipient=replace(request.recipient, zip_code="invalid"))
    else:
        request = replace(
            request, options=LetterOptions(MailClass.FIRST_CLASS, return_receipt=True)
        )
    wire = OfflineLob(monkeypatch, unavailable)
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        case = prepared(store)
        history = store.transition_history(case.case_token)
        effects = store.effects(case.case_token)
        for _ in range(2):
            with pytest.raises(ValueError, match=r".+"):
                wire.client.create_letter(request, "invalid-local-request")
            with pytest.raises(ValueError, match=r".+"):
                store.prepare(case.case_token, 2, request, NOW)
        assert not wire.requests
        assert store.get(case.case_token, 2) is None
        assert store.submission_cursor(case.case_token, 1) is None
        assert store.transition_history(case.case_token) == history
        assert store.effects(case.case_token) == effects


@pytest.mark.parametrize("invalid", ["sender-name", "recipient-line1"])
def test_sdk_local_validation_failure_is_terminal_instead_of_an_uncertain_retry_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    request = make_request()
    if invalid == "sender-name":
        request = replace(request, sender=replace(request.sender, recipient="N" * 41))
    else:
        request = replace(request, recipient=replace(request.recipient, line1="A" * 201))
    # These are limits in the installed SDK's AddressEditable model, not a claim
    # about Lob's live validation rules. The application currently permits storage.
    validate_request(request)
    wire = OfflineLob(monkeypatch, unavailable)
    payments = FakePaymentClient()
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = prepared_request(store, request)
    now = NOW
    for _ in range(3):
        with SqliteTransactionStore(path) as store:
            dispatch_once(store, wire.client, payments, clock_at(now), new_id)
            cursor = cursor_for(store, case)
            now = max(now, cursor.next_at)
    with SqliteTransactionStore(path) as store:
        assert not wire.requests  # This failure occurred locally, before any HTTP.
        assert not isinstance(cursor_for(store, case).outcome, Uncertain), (
            "The SDK rejected locally, but the scheduler keeps retrying a provider non-call"
        )
        assert cursor_for(store, case).sends == 1
        assert current_case(store, case).letter_state is cs.CaseState.STOPPED_NO_CHARGE
        assert not any(call[0] == "capture" for call in payments.calls)


def historical_acceptance(
    store: SqliteTransactionStore, case: CaseRecord, letter_id: str, now: datetime
) -> CaseRecord:
    """Seed an already-established historical acceptance to isolate recovery policies."""
    case = current_case(store, case)
    cursor = store.submission_cursor(case.case_token, case.attempt)
    with store.submission_ownership():
        claimed, _kind = store.claim_submission(
            case.case_token,
            case.attempt,
            case.revision,
            None if cursor is None else cursor.revision,
            now=now,
            operation_id=new_id(),
        )
        store.finish_submission(
            claimed, Accepted(letter_id), now=now, effect_ids=(), new_effect_id=new_id
        )
    return current_case(store, case)


def returned_case(store: SqliteTransactionStore, wire: OfflineLob) -> CaseRecord:
    case = historical_acceptance(store, prepared(store), ORIGINAL, NOW)
    payments = FakePaymentClient()
    for _ in range(2):
        dispatch_once(store, wire.client, payments, lambda: NOW, new_id)
    case = current_case(store, case)
    assert case.payment_state is ps.PaymentState.CAPTURED
    case = letter(store, case, cs.ReturnConfirmed(UspsReturnReason.NO_SUCH_NUMBER))
    cause = classify_return(
        RECIPIENT.line1, CORRECTED.line1, UspsReturnReason.NO_SUCH_NUMBER, False
    )
    assert cause is ReturnCause.OUR_ERROR
    remedy = decide_remedy(cause, True, True, case.amount_cents, 800, case.attempt - 1)
    assert remedy.remails
    return letter(store, case, cs.RemailRequested())


def test_one_free_remail_uses_persisted_verified_correction_and_survives_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = OfflineLob(monkeypatch, unavailable)
    payments = FakePaymentClient()
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = returned_case(store, wire)
        decision = seed_destination(store, case, NOW, CORRECTED)
        assert decision.provenance is AddressProvenance.SYSTEM_VERIFIED
        assert decision.fingerprint != address_fingerprint(RECIPIENT)
        request = replace(
            make_request(pdf=b"%PDF-1.4\ncorrected synthetic packet", token="remail-token"),
            recipient=CORRECTED,
        )
        store.prepare(case.case_token, 2, request, NOW)
        seed_approval(store, case.case_token, 2)
        original = store.get(case.case_token, 1)
        first = submit(store, wire, case, NOW)
    with SqliteTransactionStore(path) as store:
        saved = store.latest_destination_decision(decision.case_id, decision.owner)
        assert saved is not None
        assert saved.decision == decision
        retried = submit(store, wire, case, first.next_at)
        assert isinstance(retried.outcome, Uncertain)
        assert_frozen_create(wire, store, case)
        assert len(wire.creates) == 2
        for _, _, headers, body in wire.creates:
            to = json.loads(_parse_multipart(headers["content-type"], body)["to"])
            assert to["address_line1"] == CORRECTED.line1
            assert to["address_line1"] != RECIPIENT.line1
        assert store.get(case.case_token, 1) == original
        case = historical_acceptance(store, case, "ltr_Remail", retried.next_at)
        case = letter(store, case, cs.ReturnConfirmed(UspsReturnReason.NO_SUCH_NUMBER))
    with SqliteTransactionStore(path) as store:
        case = current_case(store, case)
        assert case.attempt == 2
        assert not decide_remedy(
            ReturnCause.OUR_ERROR, True, True, case.amount_cents, 800, 1
        ).remails
        before = store.effects(case.case_token)
        with pytest.raises(IllegalTransition):
            letter(store, case, cs.RemailRequested())
        dispatch_once(store, wire.client, payments, lambda: retried.next_at, new_id)
        assert len(wire.creates) == 2
        assert len({request[2]["idempotency-key"] for request in wire.creates}) == 1
        with pytest.raises(ValueError, match="attempt must be the integer 1 or 2"):
            store.prepare(case.case_token, 3, make_request(token="forbidden-third-attempt"), NOW)
        assert sum(row.effect is ps.PaymentEffect.CAPTURE_PAYMENT for row in before) == 1
        assert not payments.calls
        assert current_case(store, case).payment_state is ps.PaymentState.CAPTURED


@pytest.mark.parametrize(
    "destination", ["old-address", "unverified-correction", "consented-unverified"]
)
def test_wrong_address_return_cannot_remail_without_a_verified_different_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, destination: str
) -> None:
    wire = OfflineLob(monkeypatch, unavailable)
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = returned_case(store, wire)
        address = RECIPIENT if destination == "old-address" else CORRECTED
        if destination == "consented-unverified":
            intake = store.intake_for_workspace(case.workspace_token)
            assert intake is not None
            saved = store.latest_destination_decision(intake.intake_id, intake.owner)
            assert saved is not None
            # Supervisor decision (T17 piece A): the T30 customer-write lock stays, so the
            # customer address/consent/CUSTOMER_DIRECTED decision are seeded as historical
            # rows exactly as the T30 tests do. The refused-create assertions are unchanged.
            with pytest.raises(IntakeConflict, match="destination_locked_after_draft"):
                store.save_customer_address(intake.intake_id, intake.owner, address, NOW, None)
            consent = ConsentEvent(
                intake.intake_id,
                intake.owner,
                address_fingerprint(address),
                "a" * 64,
                1,
                True,
                NOW,
            )
            database = store._require_open()
            database.execute(
                "INSERT INTO pb_address_revisions VALUES (?, ?, ?, ?)",
                (
                    intake.intake_id,
                    intake.owner,
                    1,
                    _json({"address": asdict(address), "created_at": NOW}),
                ),
            )
            database.execute(
                "INSERT INTO pb_address_consents VALUES (?, ?, ?, ?)",
                (intake.intake_id, intake.owner, 1, _json(asdict(consent))),
            )
            decision = DestinationDecision(
                intake.intake_id,
                intake.owner,
                address,
                address_fingerprint(address),
                AddressProvenance.CUSTOMER_DIRECTED_UNVERIFIED,
                "address-responsibility-v1",
                NOW,
                failed_verification_code="NO_OFFICIAL_DESTINATION",
                customer_address_revision=1,
                disclosure=Disclosure("synthetic-disclosure-v1", "a" * 64),
                consent=consent,
            )
            database.execute(
                "INSERT INTO pb_address_decisions VALUES (?, ?, ?, ?)",
                (intake.intake_id, intake.owner, saved.revision + 1, _json(asdict(decision))),
            )
        store.prepare(
            case.case_token,
            2,
            replace(
                make_request(pdf=b"%PDF-1.4\ncorrected synthetic packet", token="remail-token"),
                recipient=address,
            ),
            NOW,
        )
    with SqliteTransactionStore(path) as store:
        submit(store, wire, case, NOW)
        assert not wire.creates, (
            "Wrong-address re-mail reached Lob without a SYSTEM_VERIFIED corrected fingerprint"
        )
        assert current_case(store, case).payment_state is ps.PaymentState.CAPTURED


@pytest.mark.parametrize("recovery", ["uncertain", "transient"])
def test_capture_recovery_never_creates_another_letter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: str
) -> None:
    wire = OfflineLob(monkeypatch, unavailable)
    payments = FakePaymentClient()
    payments.responses = [PaymentOutcome(recovery, "auth")]
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        case = historical_acceptance(store, prepared(store), ORIGINAL, NOW)
        frozen = store.get(case.case_token, 1)
        for _ in range(2):
            dispatch_once(store, wire.client, payments, lambda: NOW, new_id)
        assert any(call[0] == "capture" for call in payments.calls)
        cursor = store.payment_cursor(case.case_token)
        assert cursor is not None
        assert not cursor.done
        if recovery == "uncertain":
            # The lost capture response is later reconciled on the same payment object.
            payments.object = full_outcome(
                PaymentOutcome("succeeded", "auth", case.amount_cents, "charge-original")
            )
    for _ in range(6):
        with SqliteTransactionStore(path) as store:
            cursor = store.payment_cursor(case.case_token)
            assert cursor is not None
            dispatch_once(store, wire.client, payments, clock_at(cursor.next_at), new_id)
            assert not wire.requests
            assert store.get(case.case_token, 1) == frozen
            assert store.get(case.case_token, 2) is None
            assert current_case(store, case).attempt == 1
    with SqliteTransactionStore(path) as store:
        assert current_case(store, case).payment_state is ps.PaymentState.CAPTURED
        assert (
            sum(row.effect is cs.Effect.SUBMIT_LETTER for row in store.effects(case.case_token))
            == 1
        )
        assert all(call[1] == "auth" for call in payments.calls)
