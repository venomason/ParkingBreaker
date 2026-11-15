"""Restore actual snapshots around original-object side effects, then resume one dispatcher."""

import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from parkingbreaker import recovery
from parkingbreaker.config import ConfigError, ProviderMode, Settings, build_services
from parkingbreaker.effect_dispatch import dispatch_once
from parkingbreaker.lob_fakes import FakeLobClient
from parkingbreaker.lob_submission import LetterEvidence, LobResponse
from parkingbreaker.payment_fakes import FakePaymentClient
from parkingbreaker.prices import CERTIFIED_CENTS
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_payment_coordinator import step as payment_step
from test_submission_coordinator import NOW, prepared
from test_submission_coordinator import step as submission_step


def new_id() -> str:
    return uuid.uuid4().hex


@pytest.mark.parametrize(
    "boundary", ["before_intent", "after_intent", "after_provider", "after_commit"]
)
def test_restore_near_letter_boundary_never_creates_second_letter(
    tmp_path: Path, boundary: str
) -> None:
    data = tmp_path / "authority"
    data.mkdir()
    lob = FakeLobClient([LobResponse(200, "lob-original", evidence=LetterEvidence.PRODUCTION)])
    payments = FakePaymentClient()
    bundle = tmp_path / "bundle"
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        case = prepared(store)
        if boundary == "before_intent":
            recovery.backup(data, bundle)
            submission_step(store, lob, case, effects=("sent", "capture"))
        elif boundary in {"after_intent", "after_provider"}:
            with store.submission_ownership():
                store.claim_submission(
                    case.case_token, 1, case.revision, None, now=NOW, operation_id="original-op"
                )
            packet = store.get(case.case_token, 1)
            assert packet is not None
            if boundary == "after_intent":
                recovery.backup(data, bundle)
            lob.create_letter(packet.request, packet.key)
            if boundary == "after_provider":
                recovery.backup(data, bundle)
        else:
            submission_step(store, lob, case, effects=("sent", "capture"))
            recovery.backup(data, bundle)
    restored = tmp_path / "restored"
    recovery.restore(bundle, restored)
    report = recovery.reconcile_restored(
        restored, lob, payments, now=NOW + timedelta(minutes=2), new_id=new_id
    )
    assert report["status"] == "READY_FAKE", report
    with SqliteTransactionStore(restored / recovery.DATABASE) as store:
        for _ in range(3):
            dispatch_once(store, lob, payments, lambda: NOW + timedelta(minutes=5), new_id)
    assert len(lob.calls) == 1
    assert set(lob.letters.values()) == {"lob-original"}
    assert payments.captures == 1


@pytest.mark.parametrize(
    "boundary", ["before_intent", "after_intent", "after_provider", "after_commit"]
)
def test_restore_near_capture_boundary_never_captures_twice(tmp_path: Path, boundary: str) -> None:
    data = tmp_path / "authority"
    data.mkdir()
    lob = FakeLobClient([LobResponse(200, "lob-original", evidence=LetterEvidence.PRODUCTION)])
    payments = FakePaymentClient()
    bundle = tmp_path / "bundle"
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        case = prepared(store)
        submission_step(store, lob, case, effects=("sent", "capture"))
        if boundary == "before_intent":
            recovery.backup(data, bundle)
            cursor = payment_step(store, payments, case)
            payment_step(store, payments, case, cursor)
        else:
            cursor = payment_step(store, payments, case)
            current_case = store.get_case(case.case_token)
            assert current_case is not None
            with store.submission_ownership():
                claimed = store.claim_payment(
                    case.case_token,
                    current_case.revision,
                    cursor.revision,
                    now=NOW,
                    operation_id="original-capture-op",
                    key=cursor.key,
                    retry_effect_id="retry-original",
                )
            assert claimed.mutation_key is not None
            if boundary == "after_intent":
                recovery.backup(data, bundle)
            outcome = payments.capture(
                case.authorization_ref, CERTIFIED_CENTS, claimed.mutation_key, final_capture=True
            )
            if boundary == "after_provider":
                recovery.backup(data, bundle)
            elif boundary == "after_commit":
                with store.submission_ownership():
                    store.finish_payment(claimed, outcome, now=NOW)
                recovery.backup(data, bundle)
    restored = tmp_path / "restored"
    recovery.restore(bundle, restored)
    report = recovery.reconcile_restored(
        restored, lob, payments, now=NOW + timedelta(minutes=2), new_id=new_id
    )
    assert report["status"] == "READY_FAKE", report
    with SqliteTransactionStore(restored / recovery.DATABASE) as store:
        dispatch_once(store, lob, payments, lambda: NOW + timedelta(minutes=3), new_id)
        dispatch_once(store, lob, payments, lambda: NOW + timedelta(minutes=4), new_id)
    assert payments.captures == 1
    assert len([c for c in payments.calls if c[0] == "capture"]) == 1
    assert len(lob.calls) == 1


def test_quarantine_blocks_service_start_and_real_objects_never_called(tmp_path: Path) -> None:
    (tmp_path / recovery.QUARANTINE).write_text("{}")
    settings = Settings(ProviderMode.FAKE, tmp_path, "synthetic-secret", {})
    with pytest.raises(ConfigError, match="reconciliation"):
        build_services(settings)

    # Passing real-mode interfaces must not make any call or remove quarantine.
    class NoCalls:
        def __getattr__(self, name: str) -> object:
            raise AssertionError("external call forbidden: " + name)

    report = recovery.reconcile_restored(tmp_path, NoCalls(), NoCalls(), now=NOW, new_id=new_id)  # type: ignore[arg-type]
    assert report["status"] == "NEEDS_EXTERNAL"
    assert (tmp_path / recovery.QUARANTINE).exists()


def test_inconsistent_original_keeps_restore_quarantined(tmp_path: Path) -> None:
    data = tmp_path / "authority"
    data.mkdir()
    with SqliteTransactionStore(data / recovery.DATABASE) as store:
        prepared(store)
    recovery.backup(data, tmp_path / "bundle")
    restored = tmp_path / "restored"
    recovery.restore(tmp_path / "bundle", restored)
    report = recovery.reconcile_restored(
        restored, FakeLobClient(), FakePaymentClient("wrong-original"), now=NOW, new_id=new_id
    )
    assert report["status"] == "BLOCKED"
    assert (restored / recovery.QUARANTINE).exists()


@pytest.mark.parametrize("mode", [ProviderMode.FAKE, ProviderMode.TEST, ProviderMode.LIVE])
def test_environment_startup_refuses_quarantine_before_opening_the_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: ProviderMode
) -> None:
    """The API composition root must not migrate or open a restored, unreconciled database."""
    import parkingbreaker.config as config
    import parkingbreaker.transaction_store as transaction_store
    from parkingbreaker.api.app import app_from_environment
    from test_auth_integration import AUTH_VALUES

    with SqliteTransactionStore(tmp_path / recovery.DATABASE):
        pass
    (tmp_path / recovery.QUARANTINE).write_text("{}")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    opened: list[object] = []

    class Spy:
        def __init__(self, *args: object, **kwargs: object) -> None:
            opened.append(args)
            raise AssertionError("quarantined database was opened")

    settings = Settings(mode, tmp_path, "worker-secret-0123456789", AUTH_VALUES)
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(transaction_store, "SqliteTransactionStore", Spy)
    with pytest.raises(ConfigError, match="reconciliation"):
        app_from_environment()
    assert opened == []
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_fake_recovery_can_never_be_started_with_real_mode(tmp_path: Path) -> None:
    from parkingbreaker.payment_records import PaymentOutcome
    from parkingbreaker.payment_state import PaymentState
    from parkingbreaker.recovery_policy import payment_compatible

    assert not payment_compatible(PaymentState.CAPTURED, PaymentOutcome("requires_capture", "auth"))
    (tmp_path / "FAKE_RECOVERY_ONLY").write_text("offline proof")
    with pytest.raises(ConfigError, match="fake"):
        build_services(Settings(ProviderMode.TEST, tmp_path, "synthetic-secret", {}))
