"""Synthetic local observations only: no real coverage or customer replies are asserted."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest

from parkingbreaker.beta_contact_store import ContactReadiness
from parkingbreaker.beta_feedback_store import NoticePage
from parkingbreaker.beta_notice_triage import (
    OBSERVED_KINDS,
    BetaContactStore,
    BetaNoticeTriageConflict,
    BetaNoticeTriageStore,
    CohortAdmissionReadiness,
    CohortCoverageRoster,
    NoticeTriageRecord,
    ReviewedFeedbackPlan,
    evaluate_cohort_admission,
    evaluate_notice_triage,
    initialize_beta_notice_triage,
)
from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_beta_admission import NOW, campaign, candidate
from test_beta_contact_store import (
    EMAIL_A,
    EMAIL_A_CHANGED,
    ContactStore,
    challenge,
    grant,
    link_identity,
    reply,
    setup_cases,
)

# Explicit fixture IDs, not actual named human coverage.
OPERATORS = frozenset({"operator-1", "operator-2", "operator-weekend"})
LITERAL = "  Within fifteen days of receipt; confirm the date.\r\n  — agency text  "
PLAN = ReviewedFeedbackPlan(
    issuer="CA:issuer-1.gov",
    stage="initial",
    source_url="https://issuer-1.example/status",
    retrieved_on="2026-11-01",
    escalation_owner="operator-2",
    reviewed=True,
)
ROSTER = CohortCoverageRoster("operator-1", "operator-2", "operator-weekend", PLAN)


def triage_store(store: SqliteTransactionStore) -> BetaNoticeTriageStore:
    return BetaNoticeTriageStore(store._require_open(), authorized_operators=OPERATORS)


def notice(store: SqliteTransactionStore, number: int = 1) -> str:
    return store.record_beta_feedback_notice(
        f"case_{number}",
        f"owner-{number}",
        "notice-command",
        (NoticePage("page", "photo_" + "a" * 64),),
        NOW,
        stated_deadline_text=LITERAL,
    ).notice_id


def record(
    store: BetaNoticeTriageStore,
    notice_id: str,
    **overrides: Any,
) -> NoticeTriageRecord:
    """Overrides intentionally include malformed runtime inputs for boundary tests."""
    fields: dict[str, Any] = {
        "notice_id": notice_id,
        "case_id": "case_1",
        "owner": "owner-1",
        "operator_id": "operator-1",
        "command_id": "triage-command",
        "observed_kind": "procedural_deficiency",
        "literal_stated_deadline_text": LITERAL,
        "action_deadline_date": None,
        "needs_confirmation": True,
        "escalation_owner": None,
        "evidence_ref": "private:notice-review-1",
        "observed_at": NOW,
        "recorded_at": NOW,
    }
    fields.update(overrides)
    return store.record(**fields)


@pytest.fixture
def populated(tmp_path: Path) -> Generator[tuple[ContactStore, BetaNoticeTriageStore, str]]:
    with ContactStore(tmp_path / "cases.sqlite") as store:
        setup_cases(store, 2)
        link_identity(store, "owner-1", EMAIL_A)
        yield store, triage_store(store), notice(store)


def ready_contacts(store: ContactStore) -> None:
    permission = grant(store)
    challenge(store)
    reply(store, permission.target_id)


def admission(
    store: ContactStore,
    coverage: CohortCoverageRoster | None = ROSTER,
    **overrides: Any,
) -> CohortAdmissionReadiness:
    fields: dict[str, Any] = {
        "issuer": PLAN.issuer,
        "stage": PLAN.stage,
        "as_of": NOW.date(),
        "authorized_operators": OPERATORS,
    }
    fields.update(overrides)
    return evaluate_cohort_admission(
        BetaContactStore(store._require_open()), "case_1", "owner-1", coverage, **fields
    )


def test_exact_replay_durable_history_and_customer_rows_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with ContactStore(path) as store:
        setup_cases(store)
        notice_id = notice(store)
        triage = triage_store(store)
        before = store._require_open().execute("SELECT * FROM pb_beta_feedback_notices").fetchall()
        first = record(triage, notice_id)
        assert first.literal_stated_deadline_text == LITERAL
        assert first.observed_at == first.recorded_at == NOW
        assert record(triage, notice_id, recorded_at=NOW + timedelta(days=2)) == first
        second = record(
            triage,
            notice_id,
            command_id="correction-command",
            action_deadline_date="2026-11-20",
            needs_confirmation=False,
            observed_at=NOW + timedelta(hours=1),
            recorded_at=NOW + timedelta(hours=2),
        )
        assert second.sequence > first.sequence
        assert triage.list_records("case_1", "owner-1", "operator-2") == (first, second)
        assert triage.list_records("case_1", "owner-1", "operator-1", limit=1) == (first,)
        assert triage.list_records(
            "case_1", "owner-1", "operator-1", after_sequence=first.sequence
        ) == (second,)
        assert (
            store._require_open().execute("SELECT * FROM pb_beta_feedback_notices").fetchall()
            == before
        )
    with ContactStore(path) as store:
        triage = triage_store(store)
        assert record(triage, notice_id, recorded_at=NOW + timedelta(days=3)) == first
        assert triage.list_records("case_1", "owner-1", "operator-weekend") == (first, second)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operator_id", "operator-2"),
        ("observed_kind", "decision_notice"),
        ("literal_stated_deadline_text", LITERAL.strip()),
        ("action_deadline_date", "2026-11-20"),
        ("needs_confirmation", False),
        ("escalation_owner", "operator-weekend"),
        ("evidence_ref", "private:different"),
        ("observed_at", NOW - timedelta(seconds=1)),
        ("urgent", True),
    ],
)
def test_command_payload_conflicts(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
    field: str,
    value: object,
) -> None:
    _, triage, notice_id = populated
    base = {"action_deadline_date": "2026-11-21", "escalation_owner": "operator-2"}
    first = record(triage, notice_id, **base)
    changed: dict[str, object] = {**base, field: value}
    with pytest.raises(BetaNoticeTriageConflict, match="command_conflict"):
        record(triage, notice_id, **changed)
    assert triage.list_records("case_1", "owner-1", "operator-1") == (first,)


def test_notice_case_and_owner_binding_and_global_command_conflict(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
) -> None:
    store, triage, notice_id = populated
    second_notice = notice(store, 2)
    record(triage, notice_id)
    with pytest.raises(BetaNoticeTriageConflict, match="command_conflict"):
        record(triage, second_notice, case_id="case_2", owner="owner-2")
    for overrides in (
        {"owner": "owner-2"},
        {"notice_id": second_notice},
        {"notice_id": "0" * 32},
        {"case_id": "case_2", "owner": "owner-2"},
    ):
        fields = {"notice_id": notice_id, **overrides}
        with pytest.raises(IntakeConflict, match="not_found"):
            record(triage, **fields)
    with pytest.raises(IntakeConflict, match="not_found"):
        triage.list_records("case_1", "owner-2", "operator-1")
    assert triage.list_records("case_2", "owner-2", "operator-1") == ()


@pytest.mark.parametrize(
    "allowlist",
    [
        None,
        frozenset(),
        "operator-1",
        {"operator-1"},
        ["operator-1"],
        frozenset({7}),
        frozenset({"bad operator"}),
        frozenset({"x" * 129}),
    ],
)
def test_explicit_nonempty_well_formed_allowlist_required(
    tmp_path: Path, allowlist: object
) -> None:
    with ContactStore(tmp_path / "cases.sqlite") as store:
        database = store._require_open()
        with pytest.raises(BetaNoticeTriageConflict, match="operator_not_authorized"):
            BetaNoticeTriageStore(database, authorized_operators=cast("frozenset[str]", allowlist))
        assert (
            database.execute(
                "SELECT 1 FROM sqlite_master WHERE name='pb_beta_notice_triage'"
            ).fetchone()
            is None
        )


def test_operators_authorize_writes_reads_replay_and_escalation(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
) -> None:
    store, triage, notice_id = populated
    record(triage, notice_id)
    for overrides in ({"operator_id": "unknown"}, {"escalation_owner": "unknown"}):
        with pytest.raises(BetaNoticeTriageConflict, match="operator_not_authorized"):
            record(triage, notice_id, **overrides)
    with pytest.raises(BetaNoticeTriageConflict, match="operator_not_authorized"):
        triage.list_records("case_1", "owner-1", "unknown")
    changed_allowlist = BetaNoticeTriageStore(
        store._require_open(), authorized_operators=frozenset({"operator-2"})
    )
    with pytest.raises(BetaNoticeTriageConflict, match="operator_not_authorized"):
        record(changed_allowlist, notice_id)
    # Historical observations survive a change of authorized personnel.
    assert len(changed_allowlist.list_records("case_1", "owner-1", "operator-2")) == 1


@pytest.mark.parametrize("action", ["UPDATE", "DELETE", "REPLACE"])
def test_append_only_sqlite_triggers_even_with_recursive_triggers_disabled(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
    action: str,
) -> None:
    store, triage, notice_id = populated
    first = record(triage, notice_id)
    database = store._require_open()
    database.execute("PRAGMA recursive_triggers=OFF")
    statements = {
        "UPDATE": "UPDATE pb_beta_notice_triage SET evidence_ref='private:changed'",
        "DELETE": "DELETE FROM pb_beta_notice_triage",
        "REPLACE": (
            "INSERT OR REPLACE INTO pb_beta_notice_triage SELECT * FROM pb_beta_notice_triage"
        ),
    }
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        database.execute(statements[action])
    assert triage.list_records("case_1", "owner-1", "operator-1") == (first,)


def test_sqlite_notice_binding_rejects_cross_owner(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
) -> None:
    store, triage, notice_id = populated
    record(triage, notice_id)
    database = store._require_open()
    columns = [row[1] for row in database.execute("PRAGMA table_info(pb_beta_notice_triage)")]
    row = list(database.execute("SELECT * FROM pb_beta_notice_triage").fetchone())
    for name, value in (
        ("sequence", 100),
        ("triage_id", "0" * 32),
        ("command_id", "other"),
        ("owner", "owner-2"),
    ):
        row[columns.index(name)] = value
    with pytest.raises(sqlite3.IntegrityError, match="notice mismatch"):
        database.execute(
            f"INSERT INTO pb_beta_notice_triage VALUES ({','.join('?' for _ in row)})", row
        )


@pytest.mark.parametrize("kind", sorted(OBSERVED_KINDS))
def test_observation_categories_are_not_merits_scores(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
    kind: str,
) -> None:
    _, triage, notice_id = populated
    saved = record(triage, notice_id, observed_kind=kind, escalation_owner="operator-2")
    result = evaluate_notice_triage(saved)
    assert result.needs_confirmation
    assert result.confirmed_action_deadline_date is None
    assert result.escalation_required == (kind == "deadline_escalation")
    assert not hasattr(result, "score")
    assert not hasattr(result, "dismissed")


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"needs_confirmation": False}, "needs_confirmation_required"),
        ({"observed_kind": "deadline_escalation"}, "escalation_owner_required"),
        ({"urgent": True}, "escalation_owner_required"),
        ({"observed_kind": "dismissal_score"}, "invalid_kind"),
        ({"observed_kind": "delivered"}, "invalid_kind"),
        ({"action_deadline_date": "2026-02-30"}, "invalid_date"),
        ({"action_deadline_date": "20261120"}, "invalid_date"),
        ({"action_deadline_date": "2026-11-20T12:00:00"}, "invalid_date"),
        ({"needs_confirmation": 1}, "invalid_boolean"),
        ({"urgent": "false"}, "invalid_boolean"),
        ({"literal_stated_deadline_text": 1}, "invalid_literal_deadline"),
        ({"literal_stated_deadline_text": "a" * 1001}, "invalid_literal_deadline"),
        ({"evidence_ref": ""}, "invalid_evidence_ref"),
        ({"command_id": ""}, "invalid_command_id"),
        ({"command_id": "x" * 129}, "invalid_command_id"),
        ({"observed_at": NOW + timedelta(seconds=1)}, "future_observation"),
        ({"observed_at": NOW.replace(tzinfo=None)}, "invalid_timestamp"),
    ],
)
def test_invalid_triage_rules_write_nothing(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
    fields: dict[str, object],
    reason: str,
) -> None:
    _, triage, notice_id = populated
    with pytest.raises(BetaNoticeTriageConflict, match=reason):
        record(triage, notice_id, **fields)
    assert triage.list_records("case_1", "owner-1", "operator-1") == ()


def test_uncertain_date_and_urgent_rules(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
) -> None:
    _, triage, notice_id = populated
    uncertain = record(
        triage,
        notice_id,
        urgent=True,
        escalation_owner="operator-2",
        action_deadline_date="2026-11-20",
    )
    assert uncertain.action_deadline_date == "2026-11-20"
    evaluated = evaluate_notice_triage(uncertain)
    assert evaluated.escalation_required
    assert evaluated.escalation_owner == "operator-2"
    assert evaluated.confirmed_action_deadline_date is None
    confirmed = record(
        triage,
        notice_id,
        command_id="confirmed",
        observed_kind="deadline_escalation",
        action_deadline_date="2026-11-20",
        needs_confirmation=False,
        escalation_owner="operator-2",
    )
    assert evaluate_notice_triage(confirmed).confirmed_action_deadline_date == "2026-11-20"
    assert evaluate_notice_triage(confirmed).escalation_required
    no_deadline = record(
        triage,
        notice_id,
        command_id="no-deadline",
        literal_stated_deadline_text=None,
        needs_confirmation=False,
        observed_kind="agency_acknowledgment",
    )
    assert evaluate_notice_triage(no_deadline).confirmed_action_deadline_date is None
    with pytest.raises(BetaNoticeTriageConflict, match="needs_confirmation_required"):
        record(
            triage,
            notice_id,
            command_id="missing-date",
            observed_kind="deadline_escalation",
            escalation_owner="operator-2",
            literal_stated_deadline_text=None,
            needs_confirmation=False,
        )


def test_private_values_redacted_from_repr_and_errors(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
) -> None:
    _, triage, notice_id = populated
    saved = record(triage, notice_id)
    for value in (saved, evaluate_notice_triage(saved), ROSTER, PLAN):
        rendered = repr(value)
        assert "redacted" in rendered
        assert LITERAL not in rendered
        assert "private:notice-review-1" not in rendered
        assert "owner-1" not in rendered
    with pytest.raises(BetaNoticeTriageConflict) as refused:
        record(triage, notice_id, literal_stated_deadline_text="PRIVATE-CUSTOMER-CONTENT")
    assert "PRIVATE-CUSTOMER-CONTENT" not in str(refused.value)


def test_schema_initialization_only_adds_own_component(tmp_path: Path) -> None:
    with ContactStore(tmp_path / "cases.sqlite") as store:
        database = store._require_open()
        before_schema = set(database.execute("SELECT name, sql FROM sqlite_master"))
        before_versions = set(database.execute("SELECT * FROM pb_store_versions"))
        initialize_beta_notice_triage(database)
        initialize_beta_notice_triage(database)
        after_schema = set(database.execute("SELECT name, sql FROM sqlite_master"))
        assert before_schema <= after_schema
        added_names = {row[0] for row in after_schema - before_schema}
        assert added_names == {
            "pb_beta_notice_triage",
            "pb_beta_notice_triage_no_update",
            "pb_beta_notice_triage_no_delete",
            "pb_beta_notice_triage_no_replace",
            "pb_beta_notice_triage_notice_binding",
            "sqlite_autoindex_pb_beta_notice_triage_1",
            "sqlite_autoindex_pb_beta_notice_triage_2",
        }
        assert set(database.execute("SELECT * FROM pb_store_versions")) - before_versions == {
            ("beta_notice_triage", 1)
        }


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TRIGGER pb_beta_notice_triage_no_update",
        "DROP TRIGGER pb_beta_notice_triage_no_delete",
        "DROP TRIGGER pb_beta_notice_triage_no_replace",
        "DROP TRIGGER pb_beta_notice_triage_notice_binding",
        "ALTER TABLE pb_beta_notice_triage ADD COLUMN injected TEXT",
        "DELETE FROM pb_store_versions WHERE component='beta_notice_triage'",
        "UPDATE pb_store_versions SET version=2 WHERE component='beta_notice_triage'",
    ],
)
def test_partial_or_altered_schema_refuses_reopen(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
    statement: str,
) -> None:
    store, _, _ = populated
    database = store._require_open()
    database.execute(statement)
    before = list(database.execute("SELECT * FROM sqlite_master"))
    with pytest.raises(StorageCorruption):
        initialize_beta_notice_triage(database)
    assert list(database.execute("SELECT * FROM sqlite_master")) == before
    assert not database.in_transaction


def test_corrupt_record_is_not_silently_replayed_or_read(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
) -> None:
    store, triage, notice_id = populated
    record(triage, notice_id)
    database = store._require_open()
    ddl = database.execute(
        "SELECT sql FROM sqlite_master WHERE name='pb_beta_notice_triage_no_update'"
    ).fetchone()[0]
    database.execute("DROP TRIGGER pb_beta_notice_triage_no_update")
    database.execute("UPDATE pb_beta_notice_triage SET evidence_ref='private:tampered'")
    database.execute(ddl)
    with pytest.raises(StorageCorruption, match="invalid beta notice triage record"):
        record(triage, notice_id)
    with pytest.raises(StorageCorruption, match="invalid beta notice triage record"):
        triage.list_records("case_1", "owner-1", "operator-1")
    assert not database.in_transaction


def test_caller_transaction_is_never_committed_or_rolled_back(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
) -> None:
    store, triage, notice_id = populated
    database = store._require_open()
    database.execute("BEGIN")
    try:
        with pytest.raises(StorageCorruption, match="own transaction"):
            record(triage, notice_id)
        with pytest.raises(StorageCorruption, match="own transaction"):
            initialize_beta_notice_triage(database)
        with pytest.raises(StorageCorruption, match="own transaction"):
            triage.list_records("case_1", "owner-1", "operator-1")
        assert database.in_transaction
    finally:
        database.execute("ROLLBACK")


def test_concurrent_command_replay_saves_one_record(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite"
    with ContactStore(path) as store:
        setup_cases(store)
        notice_id = notice(store)
        triage_store(store)
    barrier = Barrier(2)

    def write(number: int) -> NoticeTriageRecord:
        with ContactStore(path) as store:
            triage = triage_store(store)
            barrier.wait(timeout=10)
            return record(triage, notice_id, recorded_at=NOW + timedelta(seconds=number))

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(write, (1, 2)))
    assert results[0] == results[1]
    with ContactStore(path) as store:
        assert triage_store(store).list_records("case_1", "owner-1", "operator-1") == (results[0],)


def test_positive_gate_is_read_only_and_rechecks_current_state(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
) -> None:
    store, _, _ = populated
    ready_contacts(store)
    database = store._require_open()
    before = database.total_changes
    assert admission(store) == CohortAdmissionReadiness(True, ())
    assert database.total_changes == before
    # Weekend duty may be explicitly assigned to an existing primary/backup operator.
    assert admission(store, replace(ROSTER, weekend_operator="operator-1")).qualified
    store.revoke_beta_contact_permission("case_1", "owner-1", "revoke", NOW, expected_revision=1)
    blocked = admission(store)
    assert not blocked.qualified
    assert "backup_contact_permission_required" in blocked.reasons


def test_login_verified_at_does_not_supply_two_way_reply(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
) -> None:
    store, _, _ = populated
    grant(store)
    session = store.consume_identity_link(
        hashlib.sha256(b"link-owner-1").hexdigest(),
        session_digest=hashlib.sha256(b"session").hexdigest(),
        csrf_digest=hashlib.sha256(b"csrf").hexdigest(),
        now=NOW,
        session_expires_at=NOW + timedelta(hours=1),
    )
    assert session is not None
    assert (
        store._require_open()
        .execute("SELECT verified_at FROM pb_identity_accounts WHERE customer_id='owner-1'")
        .fetchone()[0]
        is not None
    )
    result = admission(store)
    assert not result.qualified
    assert "primary_email_verification_required" in result.reasons
    assert "primary_email_two_way_reply_required" in result.reasons
    challenge(store)
    result = admission(store)
    assert not result.qualified
    assert "primary_email_verification_required" not in result.reasons
    assert "primary_email_two_way_reply_required" in result.reasons


def test_changed_primary_email_requires_fresh_verification_and_reply(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
) -> None:
    store, _, _ = populated
    ready_contacts(store)
    assert admission(store).qualified
    # Simulate the account update, as in the existing contact-store regressions;
    # issuing a login link cannot itself change an account's primary address.
    store._require_open().execute(
        "UPDATE pb_identity_accounts SET email=? WHERE customer_id='owner-1'",
        (EMAIL_A_CHANGED,),
    )
    status = BetaContactStore(store._require_open()).read_status("case_1", "owner-1")
    assert status.verification_evidence == status.reply_observation == "stale"
    result = admission(store)
    assert not result.qualified
    assert "primary_email_two_way_reply_required" in result.reasons
    challenge(store, "new-verification", observed_email=EMAIL_A_CHANGED)
    assert not admission(store).qualified
    assert status.target_id is not None
    reply(store, status.target_id, "new-reply", observed_email=EMAIL_A_CHANGED)
    assert admission(store).qualified


def test_missing_and_changed_backup_permissions_fail_closed(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
) -> None:
    store, _, _ = populated
    assert "backup_contact_permission_required" in admission(store).reasons
    ready_contacts(store)
    grant(store, "replace-backup", value="other-backup@example.com", expected_revision=1)
    result = admission(store)
    assert not result.qualified
    assert "primary_email_two_way_reply_required" in result.reasons


def test_phone_backup_can_qualify_only_with_primary_email_reply(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
) -> None:
    store, _, _ = populated
    permission = grant(store, channel="sms", value="+15551234567")
    challenge(store)
    assert not admission(store).qualified
    reply(store, permission.target_id)
    assert admission(store).qualified


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("primary_operator", None, "coverage_primary_operator_required"),
        ("primary_operator", "", "coverage_primary_operator_required"),
        ("primary_operator", "unknown", "coverage_primary_operator_required"),
        ("backup_operator", None, "coverage_backup_operator_required"),
        ("backup_operator", "operator-1", "coverage_primary_backup_must_differ"),
        ("weekend_operator", None, "coverage_weekend_operator_required"),
        ("weekend_operator", "", "coverage_weekend_operator_required"),
        ("weekend_operator", "unknown", "coverage_weekend_operator_required"),
        ("reviewed_feedback_plan", None, "reviewed_feedback_plan_required"),
    ],
)
def test_coverage_missing_unknown_or_nondistinct_fails_closed(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
    field: str,
    value: object,
    reason: str,
) -> None:
    store, _, _ = populated
    ready_contacts(store)
    changes: dict[str, Any] = {field: value}
    result = admission(store, replace(ROSTER, **changes))
    assert not result.qualified
    assert reason in result.reasons


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewed", False),
        ("reviewed", 1),
        ("issuer", "another-issuer"),
        ("stage", "appeal"),
        ("source_url", ""),
        ("source_url", "javascript:alert(1)"),
        ("source_url", "https://user:password@example.com/status"),
        ("source_url", "https://example.com/\nstatus"),
        ("retrieved_on", "2026-02-30"),
        ("retrieved_on", "2026-12-01"),
        ("retrieved_on", "20261101"),
        ("escalation_owner", "unknown"),
        ("escalation_owner", ""),
    ],
)
def test_feedback_plan_is_reviewed_complete_and_bound_to_issuer_stage(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
    field: str,
    value: object,
) -> None:
    store, _, _ = populated
    ready_contacts(store)
    changes: dict[str, Any] = {field: value}
    coverage = replace(ROSTER, reviewed_feedback_plan=replace(PLAN, **changes))
    result = admission(store, coverage)
    assert not result.qualified
    assert "reviewed_feedback_plan_required" in result.reasons


def test_absent_roster_allowlist_and_wrong_owner_are_not_qualified(
    populated: tuple[ContactStore, BetaNoticeTriageStore, str],
) -> None:
    store, _, _ = populated
    ready_contacts(store)
    assert not admission(store, None).qualified
    assert not admission(store, authorized_operators=frozenset()).qualified
    assert not admission(store, authorized_operators="operator-1").qualified
    result = evaluate_cohort_admission(
        BetaContactStore(store._require_open()),
        "case_1",
        "owner-2",
        ROSTER,
        issuer=PLAN.issuer,
        stage=PLAN.stage,
        as_of=NOW.date(),
        authorized_operators=OPERATORS,
    )
    assert not result.qualified
    assert result.reasons == ("contact_status_unavailable",)


def test_unstarted_candidate_fails_closed_until_contact_store_can_bind_a_case(
    tmp_path: Path,
) -> None:
    with ContactStore(tmp_path / "candidate.sqlite") as store:
        campaign(store)
        candidate(store, 1)
        result = admission(store)
        assert not result.qualified
        assert "contact_status_unavailable" in result.reasons


def test_forged_ready_label_cannot_hide_missing_contact_evidence() -> None:
    class UntrustedStatus:
        def read_status(self, case_id: str, owner: str) -> ContactReadiness:
            return ContactReadiness(
                "revoked", 2, "target", "missing", "missing", "beta_contact_ready"
            )

    result = evaluate_cohort_admission(
        UntrustedStatus(),
        "case_1",
        "owner-1",
        ROSTER,
        issuer=PLAN.issuer,
        stage=PLAN.stage,
        as_of=NOW.date(),
        authorized_operators=OPERATORS,
    )
    assert not result.qualified
    assert "primary_email_two_way_reply_required" in result.reasons
    assert "backup_contact_permission_required" in result.reasons
