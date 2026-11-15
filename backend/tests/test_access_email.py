from __future__ import annotations

import base64
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parkingbreaker.access_email import AccessEmailService
from parkingbreaker.customer_access import token_digest
from parkingbreaker.email_client import EmailMessage, EmailOutcome, EmailReason, FakeEmailClient
from parkingbreaker.notification_store import NotificationConflict
from parkingbreaker.transaction_store import SqliteTransactionStore

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
PROVIDER_ID = "c2205dad-2cf0-4e74-9e34-cd652f1941db"
ACCEPTED = EmailOutcome("accepted", provider_id=PROVIDER_ID)
UNCERTAIN = EmailOutcome("uncertain", reason=EmailReason.TRANSPORT_ERROR)
ORIGIN = "https://portal.example.test"


def credential(number: int) -> str:
    return base64.urlsafe_b64encode(bytes([number]) * 32).decode().rstrip("=")


class RecordingEmail(FakeEmailClient):
    def __init__(self, outcomes: list[EmailOutcome]) -> None:
        super().__init__(outcomes)
        self.sent: list[tuple[EmailMessage, str]] = []

    def send(self, message: EmailMessage, key: str) -> EmailOutcome:
        self.sent.append((message, key))
        return super().send(message, key)


def service(
    store: SqliteTransactionStore,
    email: RecordingEmail,
    clock: list[datetime],
    *,
    key: str | None = None,
    origin: str = ORIGIN,
    sender: str = "access@example.test",
) -> AccessEmailService:
    return AccessEmailService(
        store,
        email,
        key=key or credential(1),
        origin=origin,
        sender_identity=sender,
        clock=lambda: clock[0],
    )


def queue(access: AccessEmailService, *, email: str = "Owner@example.test") -> str:
    request = str(uuid.uuid4())
    access.queue_link(email=email, request_id=request)
    return request


def outbox(store: SqliteTransactionStore, request: str) -> tuple[str | None, str | None]:
    row = (
        store._require_open()
        .execute(
            "SELECT outcome, completed_at FROM pb_pending_effects WHERE request_token=?",
            (request,),
        )
        .fetchone()
    )
    return row[0], row[1]


def test_queued_intent_sends_once_and_contains_fragment_only_link(tmp_path: Path) -> None:
    email = RecordingEmail([ACCEPTED])
    clock = [NOW]
    with SqliteTransactionStore(tmp_path / "access.sqlite") as store:
        access = service(store, email, clock)
        request = queue(access)
        assert not email.sent
        access.queue_link(email="Owner@EXAMPLE.test", request_id=request)
        assert store._require_open().execute(
            "SELECT count(*) FROM pb_identity_links"
        ).fetchone() == (1,)
        assert access.dispatch() == 1
        assert access.dispatch() == 0
        assert email.call_count == 1
        assert len(email.sent) == 1
        message, key = email.sent[0]
        note = store.access_notification(request)
        assert note is not None
        raw = message.text.split("#token=", 1)[1].splitlines()[0]
        assert token_digest(raw) == note.token_digest
        assert "/access#token=" in message.text
        assert "?token=" not in message.text
        assert key == "pb-access-" + note.effect_id
        assert raw not in "\n".join(store._require_open().iterdump())
        assert outbox(store, request)[0] == "accepted"
        for table in ["pb_intake_records", "pb_cases", "pb_workspaces"]:
            assert store._require_open().execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        assert "Owner" not in repr(access)


def test_uncertain_retry_reopens_with_same_key_payload_and_original_link(tmp_path: Path) -> None:
    path = tmp_path / "access.sqlite"
    email = RecordingEmail([UNCERTAIN, ACCEPTED])
    clock = [NOW]
    with SqliteTransactionStore(path) as store:
        access = service(store, email, clock)
        request = queue(access)
        assert access.dispatch() == 1
        assert access.dispatch() == 0
        original = store.access_notification(request)
    clock[0] += timedelta(seconds=30)
    with SqliteTransactionStore(path) as reopened:
        access = service(reopened, email, clock)
        assert access.dispatch() == 1
        assert email.sent[0] == email.sent[1]
        assert reopened.access_notification(request) == original
        rows = (
            reopened._require_open()
            .execute("SELECT attempt, outcome_json FROM pb_access_email_calls ORDER BY attempt")
            .fetchall()
        )
        assert [json.loads(row[1])["status"] for row in rows] == ["uncertain", "accepted"]
        assert outbox(reopened, request)[0] == "accepted"


def test_crash_after_provider_acceptance_replays_same_intent_without_new_email(
    tmp_path: Path,
) -> None:
    class ProcessCrash(BaseException):
        pass

    class CrashAfterAccept(RecordingEmail):
        def send(self, message: EmailMessage, key: str) -> EmailOutcome:
            result = super().send(message, key)
            if len(self.sent) == 1:
                raise ProcessCrash
            return result

    path = tmp_path / "access.sqlite"
    email = CrashAfterAccept([ACCEPTED])
    clock = [NOW]
    with SqliteTransactionStore(path) as store:
        access = service(store, email, clock)
        request = queue(access)
        with pytest.raises(ProcessCrash):
            access.dispatch()
        assert outbox(store, request)[0] is None
    clock[0] += timedelta(seconds=30)
    with SqliteTransactionStore(path) as store:
        assert service(store, email, clock).dispatch() == 1
        assert email.sent[0] == email.sent[1]
        assert email.call_count == 1
        assert outbox(store, request)[0] == "accepted"
        assert store._require_open().execute(
            "SELECT outcome_json, completed_at FROM pb_access_email_calls ORDER BY attempt"
        ).fetchall()[0] == (None, None)


@pytest.mark.parametrize("terminal", ["expired", "consumed"])
def test_expired_or_consumed_link_never_sends(tmp_path: Path, terminal: str) -> None:
    email = RecordingEmail([ACCEPTED])
    clock = [NOW]
    with SqliteTransactionStore(tmp_path / "access.sqlite") as store:
        access = service(store, email, clock)
        request = queue(access)
        note = store.access_notification(request)
        assert note is not None
        if terminal == "expired":
            clock[0] += timedelta(minutes=15)
        else:
            assert (
                store.consume_identity_link(
                    note.token_digest,
                    session_digest=token_digest(credential(8)),
                    csrf_digest=token_digest(credential(9)),
                    now=NOW,
                    session_expires_at=NOW + timedelta(days=1),
                )
                is not None
            )
        assert access.dispatch() == 0
        assert not email.sent
        assert outbox(store, request)[0] == terminal


@pytest.mark.parametrize("changed", ["key", "sender", "origin"])
def test_changed_message_or_key_cannot_replay_under_original_provider_key(
    tmp_path: Path,
    changed: str,
) -> None:
    email = RecordingEmail([UNCERTAIN, ACCEPTED])
    clock = [NOW]
    with SqliteTransactionStore(tmp_path / "access.sqlite") as store:
        access = service(store, email, clock)
        request = queue(access)
        assert access.dispatch() == 1
        clock[0] += timedelta(seconds=30)
        changed_service = service(
            store,
            email,
            clock,
            key=credential(2) if changed == "key" else None,
            sender="other@example.test" if changed == "sender" else "access@example.test",
            origin="https://other.example.test" if changed == "origin" else ORIGIN,
        )
        assert changed_service.dispatch() == 0
        assert len(email.sent) == 1
        assert outbox(store, request)[0] == "configuration_failed"


def test_same_request_new_email_refuses_and_independent_accounts_progress(tmp_path: Path) -> None:
    email = RecordingEmail([ACCEPTED, ACCEPTED])
    clock = [NOW]
    with SqliteTransactionStore(tmp_path / "access.sqlite") as store:
        access = service(store, email, clock)
        first = queue(access)
        with pytest.raises(NotificationConflict):
            access.queue_link(email="another@example.test", request_id=first)
        second = queue(access, email="Other@example.test")
        assert access.dispatch(limit=1) == 1
        assert access.dispatch(limit=1) == 1
        assert {item[0].to for item in email.sent} == {"Owner@example.test", "Other@example.test"}
        assert outbox(store, first)[0] == outbox(store, second)[0] == "accepted"


def test_live_database_owner_prevents_email_call(tmp_path: Path) -> None:
    path = tmp_path / "access.sqlite"
    email = RecordingEmail([ACCEPTED])
    clock = [NOW]
    with SqliteTransactionStore(path) as one, SqliteTransactionStore(path) as two:
        queue(service(one, email, clock))
        with one.submission_ownership():
            assert service(two, email, clock).dispatch() == 0
        assert not email.sent
        assert service(two, email, clock).dispatch() == 1


def test_expiry_settlement_cannot_be_overwritten_by_an_old_call(tmp_path: Path) -> None:
    email = RecordingEmail([])
    clock = [NOW]
    with SqliteTransactionStore(tmp_path / "access.sqlite") as store:
        request = queue(service(store, email, clock))
        with store.submission_ownership():
            call = store.claim_access_email(
                request,
                operation_id=str(uuid.uuid4()),
                message_sha256="a" * 64,
                now=NOW,
            )
        assert call is not None
        clock[0] += timedelta(minutes=15)
        with store.submission_ownership():
            assert (
                store.claim_access_email(
                    request,
                    operation_id=str(uuid.uuid4()),
                    message_sha256="a" * 64,
                    now=clock[0],
                )
                is None
            )
            with pytest.raises(NotificationConflict, match="stale"):
                store.finish_access_email(call, ACCEPTED, now=clock[0])
        assert outbox(store, request)[0] == "expired"
        assert store._require_open().execute(
            "SELECT outcome_json FROM pb_access_email_calls"
        ).fetchall() == [(None,)]


def test_failed_intent_commit_makes_no_call_and_preserves_pending_link(tmp_path: Path) -> None:
    email = RecordingEmail([ACCEPTED])
    clock = [NOW]
    with SqliteTransactionStore(tmp_path / "access.sqlite") as store:
        access = service(store, email, clock)
        request = queue(access)
        db = store._require_open()
        db.execute(
            "CREATE TRIGGER fail_call BEFORE INSERT ON pb_access_email_calls "
            "BEGIN SELECT RAISE(ROLLBACK, 'synthetic failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
            access.dispatch()
        assert not email.sent
        assert outbox(store, request) == (None, None)
        assert db.execute("SELECT claimed_at FROM pb_pending_effects").fetchone() == (None,)
        db.execute("DROP TRIGGER fail_call")
        assert access.dispatch() == 1


def test_clock_regression_cannot_append_call_before_previous_completion(tmp_path: Path) -> None:
    email = RecordingEmail([])
    clock = [NOW]
    with SqliteTransactionStore(tmp_path / "access.sqlite") as store:
        request = queue(service(store, email, clock))
        with store.submission_ownership():
            call = store.claim_access_email(
                request,
                operation_id=str(uuid.uuid4()),
                message_sha256="a" * 64,
                now=NOW,
            )
            assert call is not None
            store.finish_access_email(call, UNCERTAIN, now=NOW + timedelta(minutes=1))
            with pytest.raises(NotificationConflict, match="backwards"):
                store.claim_access_email(
                    request,
                    operation_id=str(uuid.uuid4()),
                    message_sha256="a" * 64,
                    now=NOW + timedelta(seconds=40),
                )
        assert store._require_open().execute(
            "SELECT count(*) FROM pb_access_email_calls"
        ).fetchone() == (1,)


def test_new_request_after_exhaustion_keeps_access_to_original_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise exhaustion before the independent 15-minute TTL. In production the
    # default backoff can reach expiry first; neither terminal outcome consumes access.
    monkeypatch.setattr("parkingbreaker.access_email_store.MAX_ATTEMPTS", 1)
    email = RecordingEmail([UNCERTAIN, ACCEPTED])
    clock = [NOW]
    path = tmp_path / "access.sqlite"
    with SqliteTransactionStore(path) as store:
        access = service(store, email, clock)
        original_request = queue(access)
        original = store.access_notification(original_request)
        assert original is not None
        assert access.dispatch() == 1
        clock[0] += timedelta(seconds=30)
        assert access.dispatch() == 0
        assert outbox(store, original_request)[0] == "exhausted"
    with SqliteTransactionStore(path) as store:
        access = service(store, email, clock)
        fresh_request = str(uuid.uuid4())
        access.queue_link(
            email=original.email, request_id=fresh_request, scope_id=original.customer_id
        )
        assert access.dispatch() == 1
        fresh = store.access_notification(fresh_request)
        assert fresh is not None
        assert fresh.customer_id == original.customer_id
        assert fresh.token_digest != original.token_digest
        assert outbox(store, fresh_request)[0] == "accepted"
        assert outbox(store, original_request)[0] == "exhausted"
        assert store._require_open().execute(
            "SELECT consumed_at FROM pb_identity_links WHERE token_digest=?",
            (original.token_digest,),
        ).fetchone() == (None,)
        raw = email.sent[-1][0].text.split("#token=", 1)[1].splitlines()[0]
        session = store.consume_identity_link(
            token_digest(raw),
            session_digest=token_digest(credential(8)),
            csrf_digest=token_digest(credential(9)),
            now=clock[0],
            session_expires_at=clock[0] + timedelta(days=1),
        )
        assert session is not None
        assert session.customer_id == original.customer_id


@pytest.mark.parametrize("capture_pending", [False, True])
def test_email_retries_leave_pending_mail_and_payment_work_untouched(
    tmp_path: Path, capture_pending: bool
) -> None:
    from parkingbreaker.lob_fakes import FakeLobClient
    from parkingbreaker.lob_submission import LetterEvidence, LobResponse
    from test_submission_coordinator import prepared, step

    email = RecordingEmail([UNCERTAIN, ACCEPTED])
    clock = [NOW]
    with SqliteTransactionStore(tmp_path / "access.sqlite") as store:
        case = prepared(store)
        if capture_pending:
            step(
                store,
                FakeLobClient(
                    [LobResponse(200, "ltr_original", evidence=LetterEvidence.PRODUCTION)]
                ),
                case,
                effects=("letter-capture", "payment-capture"),
            )
        effects = store.effects(case.case_token)
        history = store.transition_history(case.case_token)
        cursor = store.submission_cursor(case.case_token, 1)
        assert any(
            e.effect.value == ("CAPTURE_PAYMENT" if capture_pending else "SUBMIT_LETTER")
            for e in store.pending_effects(case.case_token)
        )
        access = service(store, email, clock)
        request = queue(access)
        assert access.dispatch() == 1
        clock[0] += timedelta(seconds=30)
        assert access.dispatch() == 1
        assert email.sent[0] == email.sent[1]
        assert outbox(store, request)[0] == "accepted"
        assert store.effects(case.case_token) == effects
        assert store.transition_history(case.case_token) == history
        assert store.submission_cursor(case.case_token, 1) == cursor
        assert store.payment_cursor(case.case_token) is None
