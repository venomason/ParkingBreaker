"""Durable beta contact permission and trusted-caller evidence on the existing database."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import cast
from uuid import uuid4

import pytest

from parkingbreaker.beta_contact_store import (
    BETA_CONTACT_DISCLOSURE_SHA256,
    BETA_CONTACT_DISCLOSURE_VERSION,
    BetaContactConflict,
    BetaContactStoreMixin,
    ContactEvidence,
    ContactPermission,
    ContactReadiness,
    ContactReply,
    initialize_beta_contact,
)
from parkingbreaker.beta_store import BETA_DISCLOSURE_SHA256
from parkingbreaker.customer_access import token_digest
from parkingbreaker.email_client import EmailOutcome
from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_beta_admission import NOW, campaign, candidate

EMAIL_A = "owner-1@example.com"
EMAIL_A_CHANGED = "owner-1-changed@example.com"
EMAIL_B = "owner-2@example.com"
BACKUP = "backup-1@example.com"
OPERATOR = "operator-1"
EVIDENCE_TRIGGER = (
    "CREATE TRIGGER pb_beta_contact_evidence_no_update BEFORE UPDATE ON "
    "pb_beta_contact_evidence BEGIN SELECT RAISE(ABORT, 'beta contact evidence is immutable'); "
    "END"
)
CONTACT_TABLES = {
    "pb_beta_contact_targets",
    "pb_beta_contact_evidence",
    "pb_beta_contact_replies",
    "pb_beta_contact_commands",
}
CONTACT_TRIGGERS = {
    "pb_beta_contact_targets_no_delete",
    "pb_beta_contact_evidence_no_update",
    "pb_beta_contact_evidence_no_delete",
    "pb_beta_contact_replies_no_update",
    "pb_beta_contact_replies_no_delete",
    "pb_beta_contact_commands_no_update",
    "pb_beta_contact_commands_no_delete",
}


def raw_string(value: object) -> str:
    """Carry a deliberately malformed runtime value past the strict type checker."""
    return cast("str", value)


def raw_operators(value: object) -> frozenset[str]:
    return cast("frozenset[str]", value)


class ContactStore(BetaContactStoreMixin, SqliteTransactionStore):
    """The existing store with only this workspace's component added, never rewired."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        initialize_beta_contact(self._require_open())

    def __enter__(self) -> ContactStore:
        self._require_open()
        return self


def setup_cases(store: ContactStore, count: int = 1) -> None:
    campaign(store)
    for number in range(1, count + 1):
        candidate(store, number)
    store.select_beta_batch("beta-1", "batch-1", "approval-1", NOW)
    for number in range(1, count + 1):
        store.start_beta_case(f"case_{number}", BETA_DISCLOSURE_SHA256, NOW)


def link_identity(store: ContactStore, owner: str, email: str) -> None:
    store.issue_identity_link(
        customer_id=owner,
        email=email,
        token_digest=hashlib.sha256(f"link-{owner}".encode()).hexdigest(),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )


def grant(
    store: ContactStore,
    command_id: str = "perm-1",
    *,
    case_id: str = "case_1",
    owner: str = "owner-1",
    channel: str = "email",
    value: str = BACKUP,
    expected_revision: int = 0,
    now: datetime = NOW,
) -> ContactPermission:
    return store.record_beta_contact_permission(
        case_id,
        owner,
        command_id,
        channel,
        value,
        now,
        disclosure_version=BETA_CONTACT_DISCLOSURE_VERSION,
        disclosure_sha256=BETA_CONTACT_DISCLOSURE_SHA256,
        expected_revision=expected_revision,
    )


def reply(
    store: ContactStore,
    target_id: str,
    command_id: str = "reply-1",
    *,
    operator: str = OPERATOR,
    operators: frozenset[str] | None = frozenset({OPERATOR}),
    observed_email: str = EMAIL_A,
) -> ContactReply:
    return store.record_beta_contact_reply(
        "case_1",
        "owner-1",
        command_id,
        target_id,
        operator,
        "reply-ref-1",
        NOW,
        NOW,
        authorized_operators=operators,
        observed_primary_email=observed_email,
    )


def challenge(
    store: ContactStore,
    command_id: str = "chal-1",
    *,
    observed_email: str = EMAIL_A,
    case_id: str = "case_1",
    owner: str = "owner-1",
    consumed_at: datetime = NOW,
    now: datetime = NOW,
) -> ContactEvidence:
    return store.record_beta_contact_challenge(
        case_id,
        owner,
        command_id,
        "challenge-ref-1",
        consumed_at,
        now,
        observed_primary_email=observed_email,
    )


def human(
    store: ContactStore,
    command_id: str = "human-1",
    *,
    operator: str = OPERATOR,
    operators: frozenset[str] | None = frozenset({OPERATOR}),
    observed_email: str = EMAIL_A,
) -> ContactEvidence:
    return store.record_beta_contact_human_verification(
        "case_1",
        "owner-1",
        command_id,
        operator,
        "verification-ref-1",
        NOW,
        NOW,
        authorized_operators=operators,
        observed_primary_email=observed_email,
    )


def test_permission_evidence_and_reply_flow_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with ContactStore(path) as store:
        setup_cases(store)
        link_identity(store, "owner-1", EMAIL_A)
        assert store.beta_contact_readiness("case_1", "owner-1") == ContactReadiness(
            "missing", 0, None, "missing", "missing", "beta_contact_permission_missing"
        )
        permission = grant(store)
        assert permission.permitted is True
        assert permission.revision == 1
        assert permission.channel == "email"
        assert BACKUP not in repr(permission)
        after_permission = store.beta_contact_readiness("case_1", "owner-1")
        assert after_permission.permission == "permitted"
        assert after_permission.permission_revision == 1
        assert after_permission.target_id == permission.target_id
        assert after_permission.verification_evidence == "missing"
        assert after_permission.reason == "beta_contact_verification_missing"

        evidence = challenge(store)
        assert evidence.kind == "challenge_consumed"
        assert EMAIL_A not in repr(evidence)
        after_evidence = store.beta_contact_readiness("case_1", "owner-1")
        assert after_evidence.verification_evidence == "supplied"
        assert after_evidence.reply_observation == "missing"
        assert after_evidence.reason == "beta_contact_reply_missing"

        recorded_reply = reply(store, permission.target_id)
        assert OPERATOR not in repr(recorded_reply)
        ready = store.beta_contact_readiness("case_1", "owner-1")
        assert ready == ContactReadiness(
            "permitted", 1, permission.target_id, "supplied", "supplied", "beta_contact_ready"
        )

    with ContactStore(path) as reopened:
        assert reopened.beta_contact_readiness("case_1", "owner-1") == ready
        later = NOW + timedelta(days=3)
        assert grant(reopened, now=later) == permission
        assert challenge(reopened, now=later) == evidence
        assert reply(reopened, permission.target_id) == recorded_reply


def test_exact_replay_and_conflicting_command_reuse(tmp_path: Path) -> None:
    with ContactStore(tmp_path / "cases.sqlite") as store:
        setup_cases(store, 2)
        link_identity(store, "owner-1", EMAIL_A)
        link_identity(store, "owner-2", EMAIL_B)
        permission = grant(store)
        assert grant(store, now=NOW + timedelta(hours=1)) == permission
        with pytest.raises(BetaContactConflict, match="command_reused"):
            grant(store, value="other@example.com", now=NOW + timedelta(hours=2))
        with pytest.raises(BetaContactConflict, match="command_reused"):
            grant(store, expected_revision=1, now=NOW + timedelta(hours=3))
        with pytest.raises(BetaContactConflict, match="command_reused"):
            grant(store, case_id="case_2", owner="owner-2", now=NOW + timedelta(hours=4))
        database = store._require_open()
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_targets").fetchone() == (1,)
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_commands").fetchone() == (1,)


def test_stale_generation_revocation_and_target_change_invalidate(tmp_path: Path) -> None:
    with ContactStore(tmp_path / "cases.sqlite") as store:
        setup_cases(store)
        link_identity(store, "owner-1", EMAIL_A)
        first = grant(store)
        challenge(store)
        reply(store, first.target_id)
        assert store.beta_contact_readiness("case_1", "owner-1").reason == "beta_contact_ready"

        revoked = store.revoke_beta_contact_permission(
            "case_1", "owner-1", "revoke-1", NOW, expected_revision=1
        )
        assert revoked.permitted is False
        assert revoked.revision == 2
        assert revoked.target_id == first.target_id
        after_revoke = store.beta_contact_readiness("case_1", "owner-1")
        assert after_revoke.permission == "revoked"
        assert after_revoke.permission_revision == 2
        assert after_revoke.reply_observation == "stale"
        assert after_revoke.reason == "beta_contact_permission_revoked"
        with pytest.raises(BetaContactConflict, match="stale_revision"):
            store.revoke_beta_contact_permission(
                "case_1", "owner-1", "revoke-stale", NOW, expected_revision=1
            )
        with pytest.raises(BetaContactConflict, match="stale_revision"):
            grant(store, command_id="perm-stale", expected_revision=0)

        changed = grant(
            store, command_id="perm-2", value="backup-2@example.com", expected_revision=2
        )
        assert changed.revision == 3
        assert changed.target_id != first.target_id
        after_change = store.beta_contact_readiness("case_1", "owner-1")
        assert after_change.permission == "permitted"
        assert after_change.permission_revision == 3
        assert after_change.verification_evidence == "supplied"
        assert after_change.reply_observation == "stale"
        assert after_change.reason == "beta_contact_reply_stale"
        with pytest.raises(BetaContactConflict, match="target_changed"):
            reply(store, first.target_id, command_id="reply-old")
        reply(store, changed.target_id, command_id="reply-new")
        assert store.beta_contact_readiness("case_1", "owner-1").reason == "beta_contact_ready"


def test_human_attestation_requires_injected_allowlist_and_defaults_deny(
    tmp_path: Path,
) -> None:
    with ContactStore(tmp_path / "cases.sqlite") as store:
        setup_cases(store)
        link_identity(store, "owner-1", EMAIL_A)
        for operators in (None, frozenset(), frozenset({"someone-else"})):
            with pytest.raises(BetaContactConflict, match="operator_not_authorized"):
                human(store, f"human-{uuid4().hex}", operators=operators)
        with pytest.raises(BetaContactConflict, match="operator_not_authorized"):
            reply(store, "0" * 32, operators=None)
        database = store._require_open()
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_evidence").fetchone() == (0,)
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_replies").fetchone() == (0,)

        evidence = human(store, "human-1")
        assert evidence.kind == "human_same_address"
        assert OPERATOR not in repr(evidence)


def test_wrong_owner_case_and_changed_primary_email(tmp_path: Path) -> None:
    with ContactStore(tmp_path / "cases.sqlite") as store:
        setup_cases(store, 2)
        link_identity(store, "owner-1", EMAIL_A)
        link_identity(store, "owner-2", EMAIL_B)
        with pytest.raises(IntakeConflict, match="not_found"):
            grant(store, owner="owner-2")
        with pytest.raises(IntakeConflict, match="not_found"):
            grant(store, case_id="case_2", owner="owner-1")
        with pytest.raises(IntakeConflict, match="not_found"):
            challenge(store, "chal-wrong", case_id="case_2")
        with pytest.raises(IntakeConflict, match="not_found"):
            store.beta_contact_readiness("case-missing", "owner-1")
        permission = grant(store)
        challenge(store)
        reply(store, permission.target_id)
        assert store.beta_contact_readiness("case_1", "owner-1").reason == "beta_contact_ready"

        store._require_open().execute(
            "UPDATE pb_identity_accounts SET email=? WHERE customer_id=?",
            (EMAIL_A_CHANGED, "owner-1"),
        )
        changed = store.beta_contact_readiness("case_1", "owner-1")
        assert changed.verification_evidence == "stale"
        assert changed.reply_observation == "stale"
        assert changed.reason == "beta_contact_verification_stale"


def test_observed_address_change_and_delayed_evidence(tmp_path: Path) -> None:
    with ContactStore(tmp_path / "cases.sqlite") as store:
        setup_cases(store)
        link_identity(store, "owner-1", EMAIL_A)
        permission = grant(store)
        database = store._require_open()

        # The domain canonicalizes; a different local-part case still names another address.
        assert challenge(store, "chal-canonical", observed_email="owner-1@EXAMPLE.COM").kind == (
            "challenge_consumed"
        )
        for action in (
            lambda: challenge(store, "chal-local-case", observed_email="OWNER-1@example.com"),
            lambda: human(store, "human-local-case", observed_email="OWNER-1@example.com"),
            lambda: reply(
                store,
                permission.target_id,
                "reply-local-case",
                observed_email="OWNER-1@example.com",
            ),
        ):
            with pytest.raises(BetaContactConflict, match="observed_email_mismatch"):
                action()

        # Delayed evidence is valid when the observation predates now and still names the
        # exact current address.
        assert (
            challenge(store, "chal-delayed", consumed_at=NOW - timedelta(hours=2)).kind
            == "challenge_consumed"
        )
        human(store, "human-delayed")
        reply(store, permission.target_id, "reply-delayed")
        assert store.beta_contact_readiness("case_1", "owner-1").reason == "beta_contact_ready"

        database.execute(
            "UPDATE pb_identity_accounts SET email=? WHERE customer_id=?",
            (EMAIL_A_CHANGED, "owner-1"),
        )
        stale = store.beta_contact_readiness("case_1", "owner-1")
        assert stale.verification_evidence == "stale"
        assert stale.reply_observation == "stale"
        assert stale.reason == "beta_contact_verification_stale"

        def counts() -> tuple[int, ...]:
            return tuple(
                database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "pb_beta_contact_commands",
                    "pb_beta_contact_evidence",
                    "pb_beta_contact_replies",
                )
            )

        before = counts()
        for action in (
            lambda: challenge(store, "stale-challenge"),
            lambda: human(store, "stale-human"),
            lambda: reply(store, permission.target_id, "stale-reply"),
        ):
            with pytest.raises(BetaContactConflict, match="observed_email_mismatch"):
                action()
        assert counts() == before

        # Observing the address that is actually current is the positive control.
        challenge(store, "chal-new-address", observed_email=EMAIL_A_CHANGED)
        human(store, "human-new-address", observed_email=EMAIL_A_CHANGED)
        reply(store, permission.target_id, "reply-new-address", observed_email=EMAIL_A_CHANGED)
        current = store.beta_contact_readiness("case_1", "owner-1")
        assert current.verification_evidence == "supplied"
        assert current.reply_observation == "supplied"
        assert current.reason == "beta_contact_ready"

        # The fingerprint binds the observed address, so reusing the command id with another
        # observation is a conflict rather than a silent rebind.
        with pytest.raises(BetaContactConflict, match="command_reused"):
            challenge(store, "chal-new-address", observed_email=EMAIL_A)


def test_malformed_operator_allowlist_denies_before_any_write(tmp_path: Path) -> None:
    with ContactStore(tmp_path / "cases.sqlite") as store:
        setup_cases(store)
        link_identity(store, "owner-1", EMAIL_A)
        permission = grant(store)
        database = store._require_open()

        bad_allowlists: tuple[frozenset[str] | None, ...] = (
            None,
            frozenset(),
            frozenset({"someone-else"}),
            raw_operators("operator-1"),
            raw_operators("operator-1-extra"),
            raw_operators(["operator-1"]),
            raw_operators(("operator-1",)),
            raw_operators({"operator-1"}),
            raw_operators({"operator-1": True}),
            raw_operators(True),
            raw_operators(1),
            raw_operators(b"operator-1"),
            frozenset({raw_string(7)}),
            frozenset({"bad operator"}),
            frozenset({"x" * 129}),
            frozenset({OPERATOR, raw_string(7)}),
        )
        for operators in bad_allowlists:
            with pytest.raises(BetaContactConflict, match="operator_not_authorized"):
                human(store, f"human-{uuid4().hex}", operators=operators)
            with pytest.raises(BetaContactConflict, match="operator_not_authorized"):
                reply(store, permission.target_id, f"reply-{uuid4().hex}", operators=operators)
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_evidence").fetchone() == (0,)
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_replies").fetchone() == (0,)
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_commands").fetchone() == (1,)

        # An exact bounded ID inside an exact frozenset remains the allowed positive control.
        human(store, "valid-human")
        reply(store, permission.target_id, "valid-reply")
        assert store.beta_contact_readiness("case_1", "owner-1").reason == "beta_contact_ready"


def test_no_case_and_invalid_inputs_write_nothing(tmp_path: Path) -> None:
    with ContactStore(tmp_path / "no-case.sqlite") as store:
        campaign(store)
        candidate(store, 1)
        link_identity(store, "owner-1", EMAIL_A)
        with pytest.raises(BetaContactConflict, match="case_required"):
            grant(store)
        database = store._require_open()
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_targets").fetchone() == (0,)


def test_malformed_and_unbounded_inputs_are_fixed_refusals(tmp_path: Path) -> None:
    with ContactStore(tmp_path / "cases.sqlite") as store:
        setup_cases(store)
        link_identity(store, "owner-1", EMAIL_A)
        bad_calls = (
            lambda: grant(store, command_id=""),
            lambda: grant(store, command_id="x" * 129),
            lambda: grant(store, command_id="bad command"),
            lambda: grant(store, channel="fax"),
            lambda: grant(store, value="not-an-email"),
            lambda: grant(store, channel="sms", value="555-0100"),
            lambda: grant(store, expected_revision=-1),
            lambda: challenge(store, observed_email=raw_string("")),
            lambda: challenge(store, observed_email=raw_string("not-an-email")),
            lambda: challenge(store, observed_email=raw_string(None)),
            lambda: challenge(store, observed_email=raw_string(7)),
            lambda: challenge(store, observed_email=raw_string("a@b")),
            lambda: store.record_beta_contact_challenge(
                "case_1",
                "owner-1",
                "chal-ref",
                "x" * 201,
                NOW,
                NOW,
                observed_primary_email=EMAIL_A,
            ),
            lambda: store.record_beta_contact_challenge(
                "case_1",
                "owner-1",
                "chal-future",
                "challenge-ref-1",
                NOW + timedelta(seconds=1),
                NOW,
                observed_primary_email=EMAIL_A,
            ),
            lambda: store.record_beta_contact_challenge(
                "case_1",
                "owner-1",
                "chal-naive",
                "challenge-ref-1",
                datetime(2026, 11, 5, 12),
                NOW,
                observed_primary_email=EMAIL_A,
            ),
            lambda: human(store, observed_email=raw_string("")),
            lambda: human(store, observed_email=raw_string("not-an-email")),
            lambda: reply(
                store,
                "0" * 32,
                observed_email=raw_string("not-an-email"),
            ),
        )
        for call in bad_calls:
            with pytest.raises(ValueError, match="contact"):
                call()
        with pytest.raises(BetaContactConflict, match="disclosure_changed"):
            store.record_beta_contact_permission(
                "case_1",
                "owner-1",
                "perm-bad",
                "email",
                BACKUP,
                NOW,
                disclosure_version=BETA_CONTACT_DISCLOSURE_VERSION,
                disclosure_sha256="0" * 64,
                expected_revision=0,
            )
        with pytest.raises(ValueError, match="disclosure version"):
            store.record_beta_contact_permission(
                "case_1",
                "owner-1",
                "perm-bad",
                "email",
                BACKUP,
                NOW,
                disclosure_version=0,
                disclosure_sha256=BETA_CONTACT_DISCLOSURE_SHA256,
                expected_revision=0,
            )
        database = store._require_open()
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_targets").fetchone() == (0,)
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_evidence").fetchone() == (0,)
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_commands").fetchone() == (0,)


def test_phone_sms_backup_coexists_with_primary_email_evidence(tmp_path: Path) -> None:
    with ContactStore(tmp_path / "cases.sqlite") as store:
        setup_cases(store)
        link_identity(store, "owner-1", EMAIL_A)
        assert store.beta_contact_readiness("case_1", "owner-1").permission == "missing"
        permission = grant(store, channel="sms", value="+15551234567")
        assert permission.channel == "sms"
        assert permission.permitted is True
        database = store._require_open()
        stored = database.execute(
            "SELECT channel, contact_value, value_sha256 FROM pb_beta_contact_targets"
        ).fetchone()
        assert stored is not None
        assert stored[0] == "sms"
        assert stored[1] == "+15551234567"

        # A phone/SMS backup never verifies the primary email: evidence still needs an exact
        # explicit primary-email observation, and a mismatch refuses without writes.
        with pytest.raises(BetaContactConflict, match="observed_email_mismatch"):
            challenge(store, "chal-wrong-address", observed_email=EMAIL_B)
        with pytest.raises(BetaContactConflict, match="observed_email_mismatch"):
            human(store, "human-wrong-address", observed_email=EMAIL_B)
        with pytest.raises(BetaContactConflict, match="observed_email_mismatch"):
            reply(store, permission.target_id, "reply-wrong-address", observed_email=EMAIL_B)
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_evidence").fetchone() == (0,)
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_replies").fetchone() == (0,)
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_commands").fetchone() == (1,)

        # Actual primary-email challenge and reply evidence are independent of the backup
        # channel and still qualify while the exact phone permission is preserved.
        challenge(store, "phone-backup-challenge")
        reply(store, permission.target_id, "phone-backup-reply")
        assert store.beta_contact_readiness("case_1", "owner-1") == ContactReadiness(
            "permitted", 1, permission.target_id, "supplied", "supplied", "beta_contact_ready"
        )
        reply_row = database.execute(
            "SELECT channel, email_sha256, value_sha256 FROM pb_beta_contact_replies"
        ).fetchone()
        assert reply_row is not None
        assert reply_row[0] == "email"
        assert reply_row[1] == hashlib.sha256(EMAIL_A.encode()).hexdigest()
        assert reply_row[1] != hashlib.sha256(b"+15551234567").hexdigest()
        assert reply_row[2] == stored[2]

        # Revocation still invalidates the reply; primary-email evidence is not a bypass.
        store.revoke_beta_contact_permission(
            "case_1", "owner-1", "revoke-phone", NOW, expected_revision=1
        )
        revoked = store.beta_contact_readiness("case_1", "owner-1")
        assert revoked.permission == "revoked"
        assert revoked.permission_revision == 2
        assert revoked.reply_observation == "stale"
        assert revoked.reason == "beta_contact_permission_revoked"


def test_login_session_verified_at_and_send_acceptance_do_not_qualify(tmp_path: Path) -> None:
    with ContactStore(tmp_path / "cases.sqlite") as store:
        setup_cases(store)
        link_identity(store, "owner-1", EMAIL_A)
        permission = grant(store)
        token = hashlib.sha256(b"second-identity-link").hexdigest()
        store.issue_identity_link(
            customer_id="owner-1",
            email=EMAIL_A,
            token_digest=token,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
        )
        session = store.consume_identity_link(
            token,
            session_digest=hashlib.sha256(b"session").hexdigest(),
            csrf_digest=hashlib.sha256(b"csrf").hexdigest(),
            now=NOW,
            session_expires_at=NOW + timedelta(hours=1),
        )
        assert session is not None
        account = (
            store._require_open()
            .execute("SELECT verified_at FROM pb_identity_accounts WHERE customer_id='owner-1'")
            .fetchone()
        )
        assert account is not None
        assert account[0] is not None

        nonce = secrets.token_urlsafe(32)
        store._submission_locked = True
        note = store.queue_access_link(
            request_id=str(uuid4()),
            effect_id=str(uuid4()),
            customer_id="owner-1",
            email=EMAIL_A,
            nonce=nonce,
            token_digest=token_digest(nonce),
            issued_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=5),
        )
        call = store.claim_access_email(
            note.request_id,
            operation_id=str(uuid4()),
            message_sha256=hashlib.sha256(b"message").hexdigest(),
            now=NOW + timedelta(seconds=2),
        )
        assert call is not None
        store.finish_access_email(
            call,
            EmailOutcome("accepted", provider_id=str(uuid4())),
            now=NOW + timedelta(seconds=3),
        )
        readiness = store.beta_contact_readiness("case_1", "owner-1")
        assert readiness.permission == "permitted"
        assert readiness.target_id == permission.target_id
        assert readiness.verification_evidence == "missing"
        assert readiness.reply_observation == "missing"
        assert readiness.reason == "beta_contact_verification_missing"


def test_concurrent_identical_command_yields_one_saved_result(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with ContactStore(path) as store:
        setup_cases(store)
        link_identity(store, "owner-1", EMAIL_A)
    ready = Barrier(2)

    def record(number: int) -> ContactPermission:
        with ContactStore(path) as store:
            ready.wait(timeout=10)
            return grant(store, now=NOW + timedelta(seconds=number))

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(record, (1, 2)))
    assert results[0] == results[1]
    with ContactStore(path) as store:
        database = store._require_open()
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_targets").fetchone() == (1,)
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_commands").fetchone() == (1,)
        assert store.beta_contact_readiness("case_1", "owner-1").permission_revision == 1


def test_concurrent_different_commands_obey_generation(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with ContactStore(path) as store:
        setup_cases(store)
        link_identity(store, "owner-1", EMAIL_A)
    ready = Barrier(2)

    def record(number: int) -> ContactPermission | str:
        with ContactStore(path) as store:
            ready.wait(timeout=10)
            try:
                return grant(store, command_id=f"perm-{number}")
            except BetaContactConflict as exc:
                return str(exc)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(record, (1, 2)))
    assert sum(isinstance(item, ContactPermission) for item in results) == 1
    assert results.count("beta_contact_stale_revision") == 1
    with ContactStore(path) as store:
        database = store._require_open()
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_targets").fetchone() == (1,)
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_commands").fetchone() == (1,)
        assert store.beta_contact_readiness("case_1", "owner-1").permission_revision == 1


def test_initializer_creates_only_its_own_component(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "plain.sqlite") as store:
        database = store._require_open()
        before_tables = {
            row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        before_triggers = {
            row[0]
            for row in database.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        before_versions = set(database.execute("SELECT component, version FROM pb_store_versions"))
        initialize_beta_contact(database)
        after_tables = {
            row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        after_triggers = {
            row[0]
            for row in database.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        after_versions = set(database.execute("SELECT component, version FROM pb_store_versions"))
        assert before_tables <= after_tables
        assert after_tables - before_tables == CONTACT_TABLES
        assert after_triggers - before_triggers == CONTACT_TRIGGERS
        assert after_versions - before_versions == {("beta_contact", 1)}
        initialize_beta_contact(database)


def test_writes_leave_other_components_unchanged(tmp_path: Path) -> None:
    tables = (
        "pb_intake_records",
        "pb_intake_owners",
        "pb_cases",
        "pb_beta_entitlements",
        "pb_identity_accounts",
        "pb_pending_effects",
    )
    with ContactStore(tmp_path / "cases.sqlite") as store:
        setup_cases(store, 2)
        link_identity(store, "owner-1", EMAIL_A)
        database = store._require_open()
        before = {
            name: database.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall() for name in tables
        }
        versions = set(database.execute("SELECT component, version FROM pb_store_versions"))
        permission = grant(store)
        challenge(store)
        reply(store, permission.target_id)
        after = {
            name: database.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall() for name in tables
        }
        assert after == before
        assert set(database.execute("SELECT component, version FROM pb_store_versions")) == versions


def test_malformed_schema_fails_closed(tmp_path: Path) -> None:
    for index, statement in enumerate(
        (
            "ALTER TABLE pb_beta_contact_evidence ADD COLUMN injected TEXT",
            "DROP TRIGGER pb_beta_contact_evidence_no_update",
            "DELETE FROM pb_store_versions WHERE component='beta_contact'",
        )
    ):
        path = tmp_path / f"broken-{index}.sqlite"
        with ContactStore(path) as store:
            setup_cases(store)
            grant(store)
        raw = sqlite3.connect(path, isolation_level=None)
        raw.execute(statement)
        raw.close()
        with pytest.raises(StorageCorruption):
            ContactStore(path)


def test_malformed_stored_rows_fail_closed_and_are_not_repaired(tmp_path: Path) -> None:
    path = tmp_path / "rows.sqlite"
    with ContactStore(path) as store:
        setup_cases(store)
        link_identity(store, "owner-1", EMAIL_A)
        permission = grant(store)
        challenge(store)
        reply(store, permission.target_id)
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("DROP TRIGGER pb_beta_contact_evidence_no_update")
    raw.execute("UPDATE pb_beta_contact_evidence SET email_sha256=?", ("0" * 64,))
    raw.execute(EVIDENCE_TRIGGER)
    raw.execute("UPDATE pb_beta_contact_targets SET value_sha256=?", ("0" * 64,))
    raw.close()
    with ContactStore(path) as store:
        with pytest.raises(StorageCorruption):
            store.beta_contact_readiness("case_1", "owner-1")
        with pytest.raises(StorageCorruption):
            grant(store, command_id="perm-2", expected_revision=1)
        database = store._require_open()
        assert database.execute("SELECT COUNT(*) FROM pb_beta_contact_targets").fetchone() == (1,)
        assert database.execute("SELECT value_sha256 FROM pb_beta_contact_targets").fetchone() == (
            "0" * 64,
        )
