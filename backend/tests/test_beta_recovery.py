"""NO_CHARGE beta recovery: mail identity is reconciled, no payment object exists.

These tests use real beta admission, the real SQLite store, actual online backup and
isolated restore, and the existing fake Lob seam. Payment adapter methods are patched on
the exact FakePaymentClient class (never a subclass, which the recovery type guard would
reject) so any attempted touch raises and is recorded. Quarantine, frozen identities,
owner-scoped photos and saved draft replay data must survive exactly.
"""

from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mail_samples import make_request, seed_approval, seed_destination
from parkingbreaker import case_state as cs
from parkingbreaker import recovery
from parkingbreaker.appeal.drafting import Draft, DraftResult, DraftSentence
from parkingbreaker.beta_store import BETA_DISCLOSURE_SHA256
from parkingbreaker.blob_store import LocalBlobStore
from parkingbreaker.case_service import _RecordedDrafter
from parkingbreaker.case_state import CaseState, Event
from parkingbreaker.config import ConfigError, ProviderMode, Settings, build_services
from parkingbreaker.draft_ownership_store import DraftOutcome
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.intake_store import authorization_request_json
from parkingbreaker.llm import Usage
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import Accepted, LetterEvidence, LobResponse
from parkingbreaker.mail_attempt_store import PreparedMailAttempt
from parkingbreaker.payment_coordinator import run_payment_step
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.payment_records import mutation_key
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.prices import CERTIFIED_CENTS
from parkingbreaker.submission_coordinator import run_submission_step
from parkingbreaker.transaction_records import CaseRecord
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace
from test_beta_admission import NOW, campaign, candidate
from test_blob_store import png

ORIGINAL_LETTER = "ltr_BetaOriginal"
PAID_LETTER = "ltr_PaidOriginal"
STABLE_ISSUE = "original-object reconciliation incomplete; keep processing disabled"
DRAFT_GENERATION = 7
DRAFT_SHA = "a" * 64


@dataclass(frozen=True)
class BetaSnapshot:
    data: Path
    bundle: Path
    lob: FakeLobClient
    case: CaseRecord
    packet: PreparedMailAttempt


class CountingDrafter:
    """A paid drafter that counts real calls; replay must never reach it again."""

    def __init__(self) -> None:
        self.calls = 0

    def draft(
        self,
        notes: str,
        references: Sequence[object],
        facts: object,
        problems: Sequence[str] = (),
    ) -> DraftResult:
        self.calls += 1
        sentence = DraftSentence(f"answer {self.calls}", ())
        return DraftResult(Draft((sentence,)), Usage())


@pytest.fixture
def forbidden_payment_touches(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, tuple[object, ...]]]:
    """Patch the exact fake class: any retrieve/capture/refund touch raises and is kept."""
    touched: list[tuple[str, tuple[object, ...]]] = []

    def forbid(name: str) -> object:
        def fail(self: FakePaymentClient, *args: object, **kwargs: object) -> object:
            touched.append((name, args))
            raise AssertionError("payment adapter touched: " + name)

        return fail

    for name in ("authorize", "retrieve", "capture", "release", "retrieve_refunds", "refund"):
        monkeypatch.setattr(FakePaymentClient, name, forbid(name))
    return touched


def _advance(
    store: SqliteTransactionStore, case: CaseRecord, events: tuple[Event, ...]
) -> CaseRecord:
    for event in events:
        case = store.apply_letter_event(
            case.case_token,
            event,
            case.revision,
            occurred_at=NOW,
            new_effect_id=lambda: uuid.uuid4().hex,
        )
    return case


def _beta_ready(
    tmp_path: Path, photo_ref: str | None = None, *, submit: bool = True
) -> tuple[Path, Path, CaseRecord, PreparedMailAttempt]:
    """Real campaign, qualification, selection, entitlement and prepared identity."""
    data = tmp_path / "authority"
    data.mkdir(exist_ok=True)
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        campaign(store)
        candidate(store, 1)
        if photo_ref is not None:
            saved = store.get_workspace("ws_1")
            assert saved is not None
            store.save_workspace(
                "ws_1",
                Workspace(position="My exact synthetic words.", photo_refs=(photo_ref,)),
                NOW,
                expected_revision=saved.revision,
            )
        store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)
        case = store.start_beta_case("case_1", BETA_DISCLOSURE_SHA256, NOW)
        assert case.payment_state is PaymentState.NO_CHARGE
        assert case.amount_cents == 0
        seed_destination(store, case, NOW)
        events: tuple[Event, ...] = (
            cs.ResearchComplete(),
            cs.DraftReady(),
            cs.CustomerApproved(),
        )
        if submit:
            events += (cs.SubmitRequested(),)
        case = _advance(store, case, events)
        assert case.letter_state is (CaseState.SUBMITTING if submit else CaseState.FROZEN)
        packet = store.prepare(case.case_token, 1, make_request(token="beta-token-1"), NOW)
        seed_approval(store, case.case_token)
    return data, tmp_path / "bundle", case, packet


def _beta_snapshot(tmp_path: Path, boundary: str) -> BetaSnapshot:
    """Snapshot at one of the four durable mailing boundaries."""
    lob = FakeLobClient([LobResponse(200, ORIGINAL_LETTER, evidence=LetterEvidence.PRODUCTION)])
    data, bundle, case, packet = _beta_ready(tmp_path)
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        if boundary == "before_intent":
            recovery.backup(data, bundle)
            run_submission_step(
                store,
                lob,
                case.case_token,
                1,
                expected_case_revision=case.revision,
                expected_cursor_revision=None,
                operation_id="beta-submission-1",
                clock=lambda: NOW,
            )
        elif boundary == "after_commit":
            run_submission_step(
                store,
                lob,
                case.case_token,
                1,
                expected_case_revision=case.revision,
                expected_cursor_revision=None,
                operation_id="beta-submission-1",
                clock=lambda: NOW,
            )
            recovery.backup(data, bundle)
        else:
            with store.submission_ownership():
                store.claim_submission(
                    case.case_token,
                    1,
                    case.revision,
                    None,
                    now=NOW,
                    operation_id="beta-intent-1",
                )
            if boundary == "after_intent":
                recovery.backup(data, bundle)
            lob.create_letter(packet.request, packet.key)
            assert boundary in {"after_intent", "after_provider"}
            if boundary == "after_provider":
                recovery.backup(data, bundle)
    return BetaSnapshot(data, bundle, lob, case, packet)


def _reconcile(
    bundle: Path,
    lob: FakeLobClient,
    payments: FakePaymentClient,
    destination: Path,
    *,
    minutes: int = 2,
) -> recovery.ReconciliationReport:
    recovery.restore(bundle, destination)
    return recovery.reconcile_restored(
        destination,
        lob,
        payments,
        now=NOW + timedelta(minutes=minutes),
        new_id=lambda: uuid.uuid4().hex,
    )


def _frozen_snapshot_with_remote_original(tmp_path: Path) -> BetaSnapshot:
    """Backup at FROZEN; later authority work creates the original remotely.

    The snapshot itself contains no submission intent or saved create outcome, so a
    remote original must not be inferred into a consistent pre-submission restore.
    """
    lob = FakeLobClient([LobResponse(200, ORIGINAL_LETTER, evidence=LetterEvidence.PRODUCTION)])
    data, bundle, case, packet = _beta_ready(tmp_path, submit=False)
    assert case.letter_state is CaseState.FROZEN
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        recovery.backup(data, bundle)
        submitting = _advance(store, case, (cs.SubmitRequested(),))
        assert submitting.letter_state is CaseState.SUBMITTING
        with store.submission_ownership():
            store.claim_submission(
                submitting.case_token,
                1,
                submitting.revision,
                None,
                now=NOW,
                operation_id="frozen-intent-1",
            )
        lob.create_letter(packet.request, packet.key)
    return BetaSnapshot(data, bundle, lob, case, packet)


TWO_DAYS_MINUTES = 2 * 24 * 60


def _clock(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


def _dispatch(
    store: SqliteTransactionStore, lob: FakeLobClient, payments: FakePaymentClient
) -> None:
    for minute in range(3):
        when = NOW + timedelta(minutes=5 + minute)
        dispatch_once(
            store,
            lob,
            payments,
            _clock(when),
            lambda: uuid.uuid4().hex,
        )


def _assert_one_original_mailing(
    store: SqliteTransactionStore,
    lob: FakeLobClient,
    snapshot: BetaSnapshot,
) -> None:
    """Same key/token/PDF/original letter, one distinct mailing, no second intent."""
    saved = store.get(snapshot.case.case_token, 1)
    assert saved == snapshot.packet
    assert saved.key == snapshot.packet.key
    assert saved.request.submission_token == snapshot.packet.request.submission_token
    assert saved.request.pdf == snapshot.packet.request.pdf
    assert {key for key, _request in lob.calls} == {snapshot.packet.key}
    assert {request.submission_token for _key, request in lob.calls} == {
        snapshot.packet.request.submission_token
    }
    assert {request.pdf for _key, request in lob.calls} == {snapshot.packet.request.pdf}
    assert set(lob.letters.values()) == {ORIGINAL_LETTER}
    assert lob.cancels == []
    assert store.get(snapshot.case.case_token, 2) is None
    case = store.get_case(snapshot.case.case_token)
    assert case is not None
    assert case.attempt == 1
    assert case.payment_state is PaymentState.NO_CHARGE
    assert [e for e in store.effects(snapshot.case.case_token) if e.machine == "payment"] == []
    assert store.payment_cursor(snapshot.case.case_token) is None


def test_lost_local_result_reconciles_from_original_inventory_without_payment(
    tmp_path: Path, forbidden_payment_touches: list[tuple[str, tuple[object, ...]]]
) -> None:
    """Provider created the beta letter; the snapshot has intent but no local result."""
    snapshot = _beta_snapshot(tmp_path, "after_intent")
    assert snapshot.case.letter_state is CaseState.SUBMITTING
    restored = tmp_path / "restored"
    payments = FakePaymentClient()

    report = _reconcile(snapshot.bundle, snapshot.lob, payments, restored)

    assert report == {"status": "READY_FAKE", "checked": 1, "issues": []}
    assert forbidden_payment_touches == []
    assert not (restored / recovery.QUARANTINE).exists()
    assert (restored / "FAKE_RECOVERY_ONLY").is_file()
    with SqliteTransactionStore(restored / recovery.DATABASE) as store:
        reopened = store.get_case(snapshot.case.case_token)
        assert reopened is not None
        assert reopened.letter_state is CaseState.IN_TRANSIT
        cursor = store.submission_cursor(snapshot.case.case_token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Accepted)
        assert cursor.outcome.letter_id == ORIGINAL_LETTER
        # Its own key/token/PDF are unchanged and no second intent exists.
        _assert_one_original_mailing(store, snapshot.lob, snapshot)
        _dispatch(store, snapshot.lob, payments)
        _assert_one_original_mailing(store, snapshot.lob, snapshot)
    assert forbidden_payment_touches == []


@pytest.mark.parametrize(
    "boundary", ["before_intent", "after_intent", "after_provider", "after_commit"]
)
def test_beta_mailing_boundaries_reconcile_to_one_original_without_payment(
    tmp_path: Path,
    boundary: str,
    forbidden_payment_touches: list[tuple[str, tuple[object, ...]]],
) -> None:
    snapshot = _beta_snapshot(tmp_path, boundary)
    restored = tmp_path / "restored"
    payments = FakePaymentClient()

    report = _reconcile(snapshot.bundle, snapshot.lob, payments, restored)

    assert report["status"] == "READY_FAKE", report
    assert report["checked"] == 1
    assert forbidden_payment_touches == []
    with SqliteTransactionStore(restored / recovery.DATABASE) as store:
        reopened = store.get_case(snapshot.case.case_token)
        assert reopened is not None
        assert reopened.letter_state is CaseState.IN_TRANSIT
        assert reopened.payment_state is PaymentState.NO_CHARGE
        _assert_one_original_mailing(store, snapshot.lob, snapshot)
        _dispatch(store, snapshot.lob, payments)
        _assert_one_original_mailing(store, snapshot.lob, snapshot)
    assert forbidden_payment_touches == []


@pytest.mark.parametrize("minutes", [2, TWO_DAYS_MINUTES])
def test_frozen_snapshot_with_remote_original_stays_quarantined(
    tmp_path: Path,
    minutes: int,
    forbidden_payment_touches: list[tuple[str, tuple[object, ...]]],
) -> None:
    """A pre-submission restore cannot adopt a remotely created original letter."""
    snapshot = _frozen_snapshot_with_remote_original(tmp_path)
    restored = tmp_path / "restored"
    payments = FakePaymentClient()
    recovery.restore(snapshot.bundle, restored)
    with SqliteTransactionStore(restored / recovery.DATABASE) as store:
        frozen = store.get_case(snapshot.case.case_token)
        assert frozen is not None
        assert frozen.letter_state is CaseState.FROZEN
        frozen_packet = store.get(snapshot.case.case_token, 1)
        assert frozen_packet == snapshot.packet
        assert store.submission_cursor(snapshot.case.case_token, 1) is None
    calls_before = list(snapshot.lob.calls)
    cancels_before = list(snapshot.lob.cancels)

    report = recovery.reconcile_restored(
        restored,
        snapshot.lob,
        payments,
        now=NOW + timedelta(minutes=minutes),
        new_id=lambda: uuid.uuid4().hex,
    )

    assert report == {"status": "BLOCKED", "checked": 0, "issues": [STABLE_ISSUE]}
    assert forbidden_payment_touches == []
    assert payments.calls == []
    assert (restored / recovery.QUARANTINE).is_file()
    assert not (restored / "FAKE_RECOVERY_ONLY").exists()
    assert snapshot.lob.calls == calls_before
    assert snapshot.lob.cancels == cancels_before
    with SqliteTransactionStore(restored / recovery.DATABASE) as store:
        frozen = store.get_case(snapshot.case.case_token)
        assert frozen is not None
        assert frozen.letter_state is CaseState.FROZEN
        assert store.get(snapshot.case.case_token, 1) == frozen_packet
        assert store.submission_cursor(snapshot.case.case_token, 1) is None
        assert [e for e in store.effects(snapshot.case.case_token) if e.machine == "payment"] == []


def test_frozen_remote_original_quarantine_blocks_build_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import parkingbreaker.config as config_module

    snapshot = _frozen_snapshot_with_remote_original(tmp_path)
    restored = tmp_path / "restored"
    report = _reconcile(snapshot.bundle, snapshot.lob, FakePaymentClient(), restored)
    assert report["status"] == "BLOCKED"
    assert (restored / recovery.QUARANTINE).is_file()

    opened: list[object] = []

    class Spy:
        def __init__(self, *args: object, **kwargs: object) -> None:
            opened.append(args)
            raise AssertionError("quarantined database was opened")

    monkeypatch.setattr(config_module, "SqliteTransactionStore", Spy)
    settings = Settings(ProviderMode.FAKE, restored, "worker-secret-0123456789", {})
    with pytest.raises(ConfigError, match="reconciliation"):
        build_services(settings)
    assert opened == []


@pytest.mark.parametrize("minutes", [2, TWO_DAYS_MINUTES])
def test_submitting_original_acceptance_recovery_holds_at_later_clock(
    tmp_path: Path,
    minutes: int,
    forbidden_payment_touches: list[tuple[str, tuple[object, ...]]],
) -> None:
    snapshot = _beta_snapshot(tmp_path, "after_intent")
    restored = tmp_path / "restored"
    payments = FakePaymentClient()
    creates_before = list(snapshot.lob.calls)

    report = _reconcile(snapshot.bundle, snapshot.lob, payments, restored, minutes=minutes)

    assert report == {"status": "READY_FAKE", "checked": 1, "issues": []}
    assert forbidden_payment_touches == []
    assert snapshot.lob.calls == creates_before
    with SqliteTransactionStore(restored / recovery.DATABASE) as store:
        reopened = store.get_case(snapshot.case.case_token)
        assert reopened is not None
        assert reopened.letter_state is CaseState.IN_TRANSIT
        cursor = store.submission_cursor(snapshot.case.case_token, 1)
        assert cursor is not None
        assert isinstance(cursor.outcome, Accepted)
        assert cursor.outcome.letter_id == ORIGINAL_LETTER
        _assert_one_original_mailing(store, snapshot.lob, snapshot)


def _paid_in_transit(store: SqliteTransactionStore) -> CaseRecord:
    store.create_owned_workspace("paid-owner", "paid-workspace", Workspace(), NOW)
    case = store.start_case("paid-workspace", "auth", NOW, NOW + timedelta(days=5))
    case = _advance(
        store,
        case,
        (cs.ResearchComplete(), cs.DraftReady(), cs.CustomerApproved(), cs.SubmitRequested()),
    )
    seed_destination(store, case, NOW)
    store.prepare(case.case_token, 1, make_request(token="paid-token-1"), NOW)
    seed_approval(store, case.case_token)
    return case


def _mixed_snapshot(tmp_path: Path) -> tuple[BetaSnapshot, CaseRecord, FakePaymentClient]:
    """Beta lost local result and a paid capture already succeeded at the provider."""
    lob = FakeLobClient(
        [
            LobResponse(200, ORIGINAL_LETTER, evidence=LetterEvidence.PRODUCTION),
            LobResponse(200, PAID_LETTER, evidence=LetterEvidence.PRODUCTION),
        ]
    )
    payments = FakePaymentClient()
    data, bundle, beta, packet = _beta_ready(tmp_path)
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        with store.submission_ownership():
            store.claim_submission(
                beta.case_token, 1, beta.revision, None, now=NOW, operation_id="beta-intent-1"
            )
        lob.create_letter(packet.request, packet.key)
        paid = _paid_in_transit(store)
        run_submission_step(
            store,
            lob,
            paid.case_token,
            1,
            expected_case_revision=paid.revision,
            expected_cursor_revision=None,
            operation_id="paid-submission-1",
            clock=lambda: NOW,
            effect_ids=("sent", "capture"),
        )
        current = store.get_case(paid.case_token)
        assert current is not None
        assert current.payment_state is PaymentState.CAPTURE_PENDING
        cursor = run_payment_step(
            store,
            payments,
            paid.case_token,
            expected_case_revision=current.revision,
            expected_cursor_revision=None,
            operation_id="paid-inspect-1",
            key="capture-original",
            clock=lambda: NOW,
            retry_effect_id="paid-retry-1",
        )
        with store.submission_ownership():
            claimed = store.claim_payment(
                paid.case_token,
                current.revision,
                cursor.revision,
                now=NOW,
                operation_id="paid-capture-1",
                key=cursor.key,
                retry_effect_id="paid-retry-2",
            )
        assert claimed.mutation_key is not None
        outcome = payments.capture(
            paid.authorization_ref,
            CERTIFIED_CENTS,
            claimed.mutation_key,
            final_capture=True,
        )
        assert outcome.status == "succeeded"
        recovery.backup(data, bundle)
    return BetaSnapshot(data, bundle, lob, beta, packet), paid, payments


def test_reconciliation_cli_exports_private_original_anchors_without_modifying_restore(
    tmp_path: Path,
) -> None:
    snapshot, paid, _ = _mixed_snapshot(tmp_path)
    restored = tmp_path / "restored"
    recovery.restore(snapshot.bundle, restored)
    before = {path: path.read_bytes() for path in restored.rglob("*") if path.is_file()}
    output = tmp_path / "operator.json"
    command = [
        sys.executable,
        "-m",
        "parkingbreaker.recovery_cli",
        "reconcile",
        "--data",
        str(restored),
        "--output",
        str(output),
    ]

    result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == 3, result.stderr
    assert json.loads(result.stdout)["status"] == "NEEDS_EXTERNAL"
    assert paid.authorization_ref not in json.loads(result.stdout).values()
    assert PAID_LETTER not in result.stdout
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    contents = output.read_text()
    report = json.loads(contents)
    assert report["status"] == "NEEDS_EXTERNAL"
    assert report["missing_mail_anchors"] == []
    assert report["unresolved_intake_authorizations"] == []
    stripe = report["stripe_payment_intents"]
    assert len(stripe) == 1
    assert stripe[0]["authorization_ref"] == paid.authorization_ref
    assert stripe[0]["case_token"] == paid.case_token
    assert stripe[0]["amount_cents"] == paid.amount_cents
    assert stripe[0]["authorized_at"] == paid.authorized_at.isoformat()
    assert stripe[0]["authorization_expires_at"] == paid.authorization_expires_at.isoformat()
    assert stripe[0]["restored_payment_state"] == "CAPTURE_PENDING"
    assert snapshot.case.authorization_ref not in contents
    assert snapshot.packet.key not in contents
    letters = {letter["case_token"]: letter for letter in report["lob_letter_attempts"]}
    assert letters[paid.case_token]["cursor_letter_id"] == PAID_LETTER
    beta = letters[snapshot.case.case_token]
    assert beta["cursor_letter_id"] is None
    assert beta["submission_token"] == snapshot.packet.request.submission_token
    assert beta["approval_manifest_sha256"]
    assert beta["approved_envelope_sha256"]
    assert beta["packet_pdf_sha256"]
    assert {path: path.read_bytes() for path in restored.rglob("*") if path.is_file()} == before

    refused = subprocess.run(command, capture_output=True, text=True, check=False)
    assert refused.returncode == 1
    assert output.read_text() == contents


def test_reconciliation_inventory_preserves_unresolved_pre_case_attempt(tmp_path: Path) -> None:
    data, bundle, _, _ = _beta_ready(tmp_path)
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        store.create_owned_workspace("pending-owner", "pending-workspace", Workspace(), NOW)
        intake = store.record_intake("pending-intake", "pending-workspace", "{}", "{}", NOW)
        store.update_intake(
            intake.intake_id, intake.revision, selected_tier="CERTIFIED", case_token=None
        )
        request_json = authorization_request_json(
            intake.intake_id, "CERTIFIED", CERTIFIED_CENTS, "pm_Original"
        )
        key = mutation_key(
            "authorization", intake.intake_id, f"CERTIFIED:{CERTIFIED_CENTS}", "pm_Original"
        )
        attempt = store.begin_authorization_attempt(
            intake.intake_id,
            key,
            "CERTIFIED",
            CERTIFIED_CENTS,
            "pm_Original",
            request_json,
            NOW,
        )
        store.record_authorization_outcome(intake.intake_id, key, "uncertain", "pi_Original", NOW)
        recovery.backup(data, bundle)

    restored = tmp_path / "restored"
    recovery.restore(bundle, restored)
    output = tmp_path / "operator.json"
    recovery.export_reconciliation_inventory(restored, output)
    report = json.loads(output.read_text())

    assert report["status"] == "NEEDS_EXTERNAL"
    assert report["stripe_payment_intents"] == []
    assert report["unresolved_intake_authorizations"] == [
        {
            "intake_id": intake.intake_id,
            "attempt_number": attempt.attempt_number,
            "authorization_ref": "pi_Original",
            "amount_cents": CERTIFIED_CENTS,
            "state": "uncertain",
            "created_at": NOW.isoformat(),
        }
    ]


def test_reconciliation_inventory_requires_completed_quarantine(tmp_path: Path) -> None:
    data = tmp_path / "authority"
    data.mkdir()
    output = tmp_path / "operator.json"
    with pytest.raises(ValueError, match="quarantined"):
        recovery.export_reconciliation_inventory(data, output)
    (data / recovery.QUARANTINE).touch()
    (data / "restore-report.json").write_text('{"records_match":false,"artifacts_match":true}')
    with pytest.raises(ValueError, match="completed restore"):
        recovery.export_reconciliation_inventory(data, output)
    assert not output.exists()


def test_mixed_snapshot_reconciles_paid_capture_and_never_asks_about_beta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, paid, payments = _mixed_snapshot(tmp_path)
    restored = tmp_path / "restored"
    retrieved: list[str] = []
    real_retrieve = FakePaymentClient.retrieve

    def recording_retrieve(self: FakePaymentClient, authorization_ref: str) -> object:
        retrieved.append(authorization_ref)
        return real_retrieve(self, authorization_ref)

    monkeypatch.setattr(FakePaymentClient, "retrieve", recording_retrieve)
    report = _reconcile(snapshot.bundle, snapshot.lob, payments, restored)

    assert report == {"status": "READY_FAKE", "checked": 2, "issues": []}
    assert retrieved == [paid.authorization_ref]
    assert snapshot.case.authorization_ref not in retrieved
    assert snapshot.case.authorization_ref not in {call[1] for call in payments.calls}
    with SqliteTransactionStore(restored / recovery.DATABASE) as store:
        beta = store.get_case(snapshot.case.case_token)
        assert beta is not None
        assert beta.payment_state is PaymentState.NO_CHARGE
        assert beta.letter_state is CaseState.IN_TRANSIT
        paid_case = store.get_case(paid.case_token)
        assert paid_case is not None
        assert paid_case.payment_state is PaymentState.CAPTURED
        assert paid_case.letter_state is CaseState.IN_TRANSIT
        cursor = store.payment_cursor(paid.case_token)
        assert cursor is not None
        assert cursor.done
        frozen = store.get(snapshot.case.case_token, 1)
        assert frozen == snapshot.packet
        assert [e for e in store.effects(snapshot.case.case_token) if e.machine == "payment"] == []
        assert store.payment_cursor(snapshot.case.case_token) is None
        before_lob_calls = len(snapshot.lob.calls)
        _dispatch(store, snapshot.lob, payments)
        # No duplicated paid capture and no second mailing of either letter.
        assert payments.captures == 1
        captures = [call for call in payments.calls if call[0] == "capture"]
        assert len(captures) == 1
        assert captures[0][1] == paid.authorization_ref
        assert captures[0][2] == CERTIFIED_CENTS
        assert len(snapshot.lob.calls) == before_lob_calls
        paid_key = next(
            key for key, request in snapshot.lob.calls if request.submission_token == "paid-token-1"
        )
        assert {key for key, _request in snapshot.lob.calls} == {
            snapshot.packet.key,
            paid_key,
        }
        assert set(snapshot.lob.letters.values()) == {ORIGINAL_LETTER, PAID_LETTER}
        assert snapshot.lob.cancels == []
        assert store.get(paid.case_token, 2) is None
        assert store.get(snapshot.case.case_token, 2) is None


def test_mixed_paid_identity_mismatch_keeps_quarantine_and_never_captures(
    tmp_path: Path,
) -> None:
    snapshot, paid, _payments = _mixed_snapshot(tmp_path)
    restored = tmp_path / "restored"
    wrong = FakePaymentClient("wrong-original")
    calls_before = list(snapshot.lob.calls)
    cancels_before = list(snapshot.lob.cancels)

    report = _reconcile(snapshot.bundle, snapshot.lob, wrong, restored)

    assert report["status"] == "BLOCKED"
    assert report["issues"] == [STABLE_ISSUE]
    assert report["checked"] == 1  # the beta case was reconciled by mail before the paid check
    assert (restored / recovery.QUARANTINE).is_file()
    assert not (restored / "FAKE_RECOVERY_ONLY").exists()
    assert [call for call in wrong.calls if call[0] in {"capture", "refund", "release"}] == []
    assert snapshot.lob.calls == calls_before
    assert snapshot.lob.cancels == cancels_before
    with SqliteTransactionStore(restored / recovery.DATABASE) as store:
        paid_case = store.get_case(paid.case_token)
        assert paid_case is not None
        assert paid_case.payment_state is PaymentState.CAPTURE_PENDING


@pytest.mark.parametrize("evidence", ["unknown", "mismatched", "missing_frozen_identity"])
def test_unresolved_beta_evidence_or_missing_identity_stays_quarantined(
    tmp_path: Path, evidence: str
) -> None:
    snapshot = _beta_snapshot(tmp_path, "after_commit")
    if evidence == "unknown":
        snapshot.lob.letters.pop(snapshot.packet.request.submission_token, None)
        snapshot.lob.evidence_by_token.pop(snapshot.packet.request.submission_token, None)
    elif evidence == "mismatched":
        snapshot.lob.letters[snapshot.packet.request.submission_token] = "ltr_Different"
    restored = tmp_path / "restored"
    recovery.restore(snapshot.bundle, restored)
    if evidence == "missing_frozen_identity":
        with sqlite3.connect(restored / recovery.DATABASE) as database:
            database.execute("PRAGMA foreign_keys=OFF")
            database.execute(
                "DELETE FROM pb_mail_attempts WHERE case_token=?", (snapshot.case.case_token,)
            )
            database.commit()
    calls_before = list(snapshot.lob.calls)
    cancels_before = list(snapshot.lob.cancels)
    payments = FakePaymentClient()

    report = recovery.reconcile_restored(
        restored,
        snapshot.lob,
        payments,
        now=NOW + timedelta(minutes=2),
        new_id=lambda: uuid.uuid4().hex,
    )

    assert report["status"] == "BLOCKED"
    assert report["checked"] == 0
    assert report["issues"] == [STABLE_ISSUE]
    assert (restored / recovery.QUARANTINE).is_file()
    assert not (restored / "FAKE_RECOVERY_ONLY").exists()
    assert snapshot.lob.calls == calls_before
    assert snapshot.lob.cancels == cancels_before
    assert payments.calls == []
    # Stable categories only: no provider object ids or synthetic customer text in output.
    report_files = sorted(restored.glob("reconciliation-*.json"))
    assert len(report_files) == 1
    saved = report_files[0].read_text()
    assert STABLE_ISSUE in saved
    assert "synthetic" not in saved
    assert "ltr_" not in saved
    alerts = (tmp_path / "recovery-alerts.jsonl").read_text().splitlines()
    assert alerts == ['{"event": "reconciliation_failed"}']


def test_active_draft_photo_and_frozen_identity_survive_backup_restore(
    tmp_path: Path,
) -> None:
    """Returned draft JSON, attempt/run counters, slot, owner photos and packet are exact."""
    data = tmp_path / "authority"
    data.mkdir()
    photos = LocalBlobStore(data / "photos")
    white = png("white")
    black = png("black")
    owner_ref = photos.put("owner-1", white)
    other_ref = photos.put("owner-2", black)
    data, bundle, case, packet = _beta_ready(tmp_path, photo_ref=owner_ref)
    inner = CountingDrafter()
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        with store.draft_ownership(case.case_token) as owned:
            assert owned
        assert store.begin_draft_run(case.case_token, DRAFT_GENERATION, NOW) == 1
        recorded = _RecordedDrafter(
            inner, store, case.case_token, DRAFT_GENERATION, DRAFT_SHA, lambda: NOW
        )
        first = recorded.draft("notes", (), None)  # type: ignore[arg-type]
        assert inner.calls == 1
        assert store.begin_draft_attempt(case.case_token, DRAFT_GENERATION, DRAFT_SHA, NOW) == 2
        store.finish_draft_attempt(
            case.case_token,
            DRAFT_GENERATION,
            2,
            DRAFT_SHA,
            DraftOutcome.FAILED,
        )
        database = store._require_open()
        slots_before = database.execute("SELECT case_token, slot FROM pb_draft_slots").fetchall()
        runs_before = database.execute(
            "SELECT case_token, generation, run FROM pb_draft_runs"
        ).fetchall()
        attempts_before = database.execute(
            "SELECT case_token, generation, attempt, inputs_sha256, outcome, result_json "
            "FROM pb_draft_attempts ORDER BY attempt"
        ).fetchall()
        recovery.backup(data, bundle)

    restored = tmp_path / "restored"
    recovery.restore(bundle, restored)
    with SqliteTransactionStore(restored / recovery.DATABASE) as store:
        reopened = store.get(case.case_token, 1)
        assert reopened == packet
        assert reopened.key == packet.key
        assert reopened.request.submission_token == packet.request.submission_token
        assert reopened.request.pdf == packet.request.pdf
        database = store._require_open()
        slots_after = database.execute("SELECT case_token, slot FROM pb_draft_slots").fetchall()
        assert slots_after == slots_before
        runs_after = database.execute(
            "SELECT case_token, generation, run FROM pb_draft_runs"
        ).fetchall()
        assert runs_after == runs_before
        attempts_after = database.execute(
            "SELECT case_token, generation, attempt, inputs_sha256, outcome, result_json "
            "FROM pb_draft_attempts ORDER BY attempt"
        ).fetchall()
        assert attempts_after == attempts_before
        with store.draft_ownership(case.case_token) as owned_again:
            assert owned_again
        slots_after = database.execute("SELECT case_token, slot FROM pb_draft_slots").fetchall()
        assert slots_after == slots_before
        fingerprint = attempts_before[0][3]
        assert isinstance(fingerprint, str)
        saved = store.returned_draft(case.case_token, DRAFT_GENERATION, fingerprint)
        assert saved is not None
        assert saved == attempts_before[0][5]
        recovered = _RecordedDrafter(
            inner, store, case.case_token, DRAFT_GENERATION, DRAFT_SHA, lambda: NOW
        )
        replayed = recovered.draft("notes", (), None)  # type: ignore[arg-type]
        assert replayed.draft == first.draft
        assert inner.calls == 1  # no added paid call: the saved result was replayed
        assert store.begin_draft_run(case.case_token, DRAFT_GENERATION, NOW) == 2
        assert store.returned_draft(case.case_token, DRAFT_GENERATION + 1, fingerprint) is None
        assert store.returned_draft(case.case_token, DRAFT_GENERATION, "b" * 64) is None
        assert store.returned_draft("another-case", DRAFT_GENERATION, fingerprint) is None

    restored_photos = LocalBlobStore(restored / "photos")
    assert restored_photos.get("owner-1", owner_ref) == white
    assert restored_photos.get("owner-2", other_ref) == black
    assert restored_photos.get("owner-2", owner_ref) is None
    assert restored_photos.get("owner-1", other_ref) is None
