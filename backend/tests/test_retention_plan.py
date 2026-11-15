"""T36 one-case read-only service-data retention dry-run plan.

These tests use synthetic SQLite databases and an in-memory blob store only. They cover
bounded keyset pagination over every workspace-history revision, stable and input-bound plan
digests, the authoritative ``pb_cases.workspace_token`` case binding (missing, mismatched and
ambiguous binding refusals, plus a real ``SqliteTransactionStore`` schema integration case),
source-schema fingerprints that bind table/view/trigger/index definitions, explicit
policy/hold/blocker refusals, conservative shared/other-owner/orphan blob classification,
unknown and malformed source protection, explicit partial plans at the plan bound,
caller-transaction refusal, unchanged database bytes, and that raw customer markers never
appear in a plan repr, the public summary or an error.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from parkingbreaker import retention_plan as rp
from parkingbreaker import retention_state as rs
from parkingbreaker.retention_plan import (
    BlobDisposition,
    PlanBlockedReason,
    RetentionPlanRefused,
    SourceState,
    TargetDisposition,
    plan_service_data_retention,
)
from parkingbreaker.retention_policy import (
    ClosureKind,
    DataScope,
    PolicyAdoption,
    UnresolvedBlockers,
)
from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.workspace import Workspace

CASE = "case-1"
WORKSPACE = "workspace-1"
INTAKE = "intake-1"
OWNER = "owner-1"
OTHER_WORKSPACE = "workspace-2"
OTHER_OWNER_WORKSPACE = "workspace-3"
OTHER_OWNER = "owner-2"
SENTINEL = "SENTINEL-CUSTOMER-STORY-9f2c"
CONTACT_SENTINEL = "CONTACT-SENTINEL-77b1"
ANCHOR = datetime(2020, 1, 1, 12, 0, tzinfo=UTC)
POLICY_AT = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
EVALUATED = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
PHOTO_BYTES = b"not really a photo, only a deterministic blob"
PHOTO_REF = "photo_" + hashlib.sha256(PHOTO_BYTES).hexdigest()

_DDL = """
CREATE TABLE pb_store_versions (component TEXT PRIMARY KEY, version INTEGER NOT NULL);
CREATE TABLE pb_workspaces (
    workspace_token TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL
);
CREATE TABLE pb_workspace_history (
    workspace_token TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_token, revision)
);
CREATE TABLE pb_intake_owners (
    workspace_token TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE pb_intake_records (
    intake_id TEXT PRIMARY KEY,
    workspace_token TEXT NOT NULL UNIQUE,
    intake_json TEXT NOT NULL,
    prehold_json TEXT NOT NULL,
    agency_json TEXT,
    selected_tier TEXT,
    case_token TEXT UNIQUE,
    created_at TEXT NOT NULL,
    revision INTEGER NOT NULL
);
CREATE TABLE pb_intake_drafts (
    intake_id TEXT PRIMARY KEY,
    letter_text TEXT NOT NULL,
    spans_json TEXT NOT NULL,
    flags_json TEXT NOT NULL,
    adopted_sha256 TEXT,
    adopted_at TEXT,
    adopted_by TEXT,
    packet_sha256 TEXT,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL
);
CREATE TABLE pb_intake_research (
    intake_id TEXT PRIMARY KEY,
    research_json TEXT NOT NULL,
    answers_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL
);
CREATE TABLE pb_draft_attempts (
    case_token TEXT NOT NULL,
    generation INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    inputs_sha256 TEXT NOT NULL,
    started_at TEXT NOT NULL,
    outcome TEXT,
    result_json TEXT,
    PRIMARY KEY (case_token, generation, attempt)
);
CREATE TABLE pb_beta_feedback_notices (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    notice_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    correction_of TEXT,
    needs_confirmation INTEGER NOT NULL,
    review_state TEXT NOT NULL,
    UNIQUE (case_id, request_id)
);
CREATE TABLE pb_ticket_intake_operations (
    request_id TEXT PRIMARY KEY,
    intake_id TEXT NOT NULL,
    workspace_token TEXT NOT NULL,
    owner TEXT NOT NULL,
    operation TEXT NOT NULL,
    binding_sha256 TEXT NOT NULL,
    claim_token_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    usage_json TEXT,
    claimed_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE pb_beta_contact_targets (
    case_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    case_token TEXT NOT NULL,
    target_id TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL,
    channel TEXT NOT NULL,
    contact_value TEXT NOT NULL,
    value_sha256 TEXT NOT NULL,
    disclosure_version INTEGER NOT NULL,
    disclosure_sha256 TEXT NOT NULL,
    permitted INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (case_id, owner)
);
CREATE TABLE pb_address_revisions (
    case_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (case_id, owner, revision)
);
CREATE TABLE pb_address_consents (
    case_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (case_id, owner, revision)
);
CREATE TABLE pb_address_decisions (
    case_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (case_id, owner, revision)
);
CREATE TABLE pb_required_copies (
    case_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    research_sha256 TEXT NOT NULL,
    selections_json TEXT NOT NULL,
    revision INTEGER NOT NULL
);
"""


class _FakeStore:
    def __init__(self, items: dict[tuple[str, str], bytes]) -> None:
        self.items = items

    def get(self, owner: str, ref: str) -> bytes | None:
        return self.items.get((owner, ref))


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


def _record_baseline(
    connection: sqlite3.Connection,
    *,
    adoption: PolicyAdoption = PolicyAdoption.ADOPTED,
    service_hold: bool = False,
) -> None:
    rs.initialize_retention_state(connection)
    rs.record_policy(
        connection,
        command_id="policy-1",
        adoption=adoption,
        timezone=ZoneInfo("UTC"),
        at=POLICY_AT,
        adoption_evidence_ref="evidence-1" if adoption is PolicyAdoption.ADOPTED else None,
    )
    rs.record_closure(
        connection,
        command_id="closure-1",
        case_token=CASE,
        kind=ClosureKind.CLOSED,
        anchor_at=ANCHOR,
        source_ref="closure-source-1",
        at=POLICY_AT,
    )
    if service_hold:
        rs.record_hold(
            connection,
            command_id="hold-1",
            case_token=CASE,
            hold_id="hold-service",
            scope=DataScope.SERVICE_DATA,
            source_ref="hold-source-1",
            at=POLICY_AT,
        )


def make_database(
    path: Path,
    *,
    history: int = 0,
    adoption: PolicyAdoption = PolicyAdoption.ADOPTED,
    service_hold: bool = False,
    same_owner_reference: bool = False,
    other_owner_reference: bool = False,
    unknown_table: bool = False,
    case_workspace: bool = False,
) -> sqlite3.Connection:
    connection = _connect(path)
    connection.executescript(_DDL)
    if case_workspace:
        connection.execute(
            "CREATE TABLE pb_cases (case_token TEXT PRIMARY KEY, workspace_token TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO pb_cases VALUES (?, ?)", (CASE, WORKSPACE))
    else:
        # The minimal shape the frozen control upgrades with ``ALTER TABLE``.
        connection.execute("CREATE TABLE pb_cases (case_token TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO pb_cases VALUES (?)", (CASE,))
    workspace_payload = json.dumps({"story": SENTINEL, "pages": [{"photo_ref": PHOTO_REF}]})
    connection.execute(
        "INSERT INTO pb_workspaces VALUES (?, ?, ?, ?)",
        (WORKSPACE, workspace_payload, "2024-01-01T00:00:00+00:00", 2),
    )
    connection.execute(
        "INSERT INTO pb_intake_owners VALUES (?, ?, ?)",
        (WORKSPACE, OWNER, "2024-01-01T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO pb_intake_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            INTAKE,
            WORKSPACE,
            json.dumps({"story": SENTINEL}),
            json.dumps({"prehold": SENTINEL}),
            None,
            "STANDARD",
            CASE,
            "2024-01-01T00:00:00+00:00",
            2,
        ),
    )
    connection.execute(
        "INSERT INTO pb_intake_drafts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (INTAKE, SENTINEL, "{}", "{}", None, None, None, None, "2024-01-01", 1),
    )
    connection.execute(
        "INSERT INTO pb_intake_research VALUES (?, ?, ?, ?, ?)",
        (INTAKE, json.dumps({"packet_plan": {"customer_fields": [SENTINEL]}}), "{}", "2024", 1),
    )
    connection.execute(
        "INSERT INTO pb_draft_attempts VALUES (?, ?, ?, ?, ?, ?, ?)",
        (CASE, 1, 1, "a" * 64, "2024-01-01", "returned", json.dumps({"text": SENTINEL})),
    )
    connection.execute(
        "INSERT INTO pb_beta_feedback_notices "
        "(case_id, owner, notice_id, request_id, received_at, payload_json, "
        "payload_sha256, needs_confirmation, review_state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'unreviewed')",
        (INTAKE, OWNER, "notice-1", "request-1", "2024", json.dumps({"text": SENTINEL}), "b" * 64),
    )
    connection.execute(
        "INSERT INTO pb_ticket_intake_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "operation-1",
            INTAKE,
            WORKSPACE,
            OWNER,
            "extract",
            "c" * 64,
            "d" * 64,
            "success",
            json.dumps({"text": SENTINEL}),
            None,
            "2024",
            "2024",
        ),
    )
    connection.execute(
        "INSERT INTO pb_beta_contact_targets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            INTAKE,
            OWNER,
            CASE,
            "target-1",
            1,
            "email",
            CONTACT_SENTINEL,
            "e" * 64,
            1,
            "f" * 64,
            1,
            "2024",
        ),
    )
    for table in ("pb_address_revisions", "pb_address_consents", "pb_address_decisions"):
        connection.execute(
            f"INSERT INTO {table} VALUES (?, ?, ?, ?)",
            (INTAKE, OWNER, 1, json.dumps({"text": SENTINEL})),
        )
    connection.execute(
        "INSERT INTO pb_required_copies VALUES (?, ?, ?, ?, ?)",
        (INTAKE, OWNER, "f" * 64, json.dumps([{"requirement_id": "a" * 64}]), 1),
    )
    if history:
        connection.executemany(
            "INSERT INTO pb_workspace_history VALUES (?, ?, ?, ?)",
            [
                (WORKSPACE, revision, json.dumps({"n": revision}), "2024")
                for revision in range(1, history + 1)
            ],
        )
    if same_owner_reference:
        connection.execute(
            "INSERT INTO pb_intake_owners VALUES (?, ?, ?)", (OTHER_WORKSPACE, OWNER, "2024")
        )
        connection.execute(
            "INSERT INTO pb_workspaces VALUES (?, ?, ?, ?)",
            (OTHER_WORKSPACE, json.dumps({"shared": PHOTO_REF}), "2024", 1),
        )
    if other_owner_reference:
        connection.execute(
            "INSERT INTO pb_intake_owners VALUES (?, ?, ?)",
            (OTHER_OWNER_WORKSPACE, OTHER_OWNER, "2024"),
        )
        connection.execute(
            "INSERT INTO pb_workspaces VALUES (?, ?, ?, ?)",
            (OTHER_OWNER_WORKSPACE, json.dumps({"other": PHOTO_REF}), "2024", 1),
        )
    if unknown_table:
        connection.execute("CREATE TABLE pb_unknown_reference_pool (payload TEXT)")
        connection.execute(
            "INSERT INTO pb_unknown_reference_pool VALUES (?)",
            (json.dumps({"ref": PHOTO_REF}),),
        )
    _record_baseline(connection, adoption=adoption, service_hold=service_hold)
    return connection


def build_plan(
    connection: sqlite3.Connection,
    *,
    blockers: UnresolvedBlockers | None = None,
    blob_store: rp.BlobStoreLike | None = None,
    batch_size: int = rp.DEFAULT_BATCH_SIZE,
    max_plan_rows: int = rp.DEFAULT_MAX_PLAN_ROWS,
) -> rp.RetentionPlan:
    return plan_service_data_retention(
        connection,
        case_token=CASE,
        evaluated_at=EVALUATED,
        blockers=UnresolvedBlockers() if blockers is None else blockers,
        blob_store=blob_store,
        batch_size=batch_size,
        max_plan_rows=max_plan_rows,
    )


def count(category: rp.TargetCategory, summary: rp.RetentionPlanSummary) -> int:
    return dict(summary.target_counts)[category.value]


def blob_disposition(plan: rp.RetentionPlan, ref: str) -> BlobDisposition:
    return next(blob.disposition for blob in plan.blobs if blob.ref == ref)


def test_eligible_plan_is_content_free_and_never_authorises_deletion(tmp_path: Path) -> None:
    connection = make_database(tmp_path / "parkingbreaker.sqlite", case_workspace=True)
    try:
        plan = build_plan(connection)
        assert plan.summary.eligible is True
        assert plan.summary.blocked_reasons == ()
        assert plan.summary.service_purge_complete is False
        assert plan.summary.deletion_authorized is False
        assert plan.summary.retention_policy_activated is False
        assert plan.summary.owner_directories_removed is False
        assert "backups" in plan.summary.outstanding_scopes
        assert "provider_held_copies" in plan.summary.outstanding_scopes
        assert plan.summary.format_version == rp.FORMAT_VERSION
        # Every ordinary target category was inventoried.
        assert count(rp.TargetCategory.WORKSPACE_PAYLOAD, plan.summary) == 1
        assert count(rp.TargetCategory.INTAKE_RECORD, plan.summary) == 3
        assert count(rp.TargetCategory.INTAKE_DRAFT, plan.summary) == 3
        assert count(rp.TargetCategory.INTAKE_RESEARCH, plan.summary) == 2
        assert count(rp.TargetCategory.ADDRESS_REVISION, plan.summary) == 1
        assert count(rp.TargetCategory.REQUIRED_COPIES, plan.summary) == 1
        # The four immutable rows are labelled for a later migration, never deletion now.
        assert (
            dict(plan.summary.disposition_counts)[TargetDisposition.REQUIRES_MIGRATION.value] == 4
        )
    finally:
        connection.close()


def test_every_workspace_history_revision_is_counted_with_bounded_batches(
    tmp_path: Path,
) -> None:
    connection = make_database(
        tmp_path / "parkingbreaker.sqlite", history=6001, case_workspace=True
    )
    try:
        plan = build_plan(connection, batch_size=500)
        assert count(rp.TargetCategory.WORKSPACE_HISTORY, plan.summary) == 6001
        assert plan.partial is False
        assert plan.summary.eligible is True
    finally:
        connection.close()


def test_plan_digest_is_stable_and_bound_to_each_changed_input(tmp_path: Path) -> None:
    connection = make_database(tmp_path / "parkingbreaker.sqlite", case_workspace=True)
    try:
        first = build_plan(connection)
        assert build_plan(connection).plan_digest == first.plan_digest

        rs.record_policy(
            connection,
            command_id="policy-2",
            adoption=PolicyAdoption.ADOPTED,
            timezone=ZoneInfo("UTC"),
            at=POLICY_AT,
            adoption_evidence_ref="evidence-2",
            expected_version=1,
        )
        after_policy = build_plan(connection)
        assert after_policy.plan_digest != first.plan_digest
        assert after_policy.policy_revision_digest != first.policy_revision_digest

        rs.record_closure(
            connection,
            command_id="closure-2",
            case_token=CASE,
            kind=ClosureKind.ABANDONED,
            anchor_at=ANCHOR,
            source_ref="closure-source-2",
            at=POLICY_AT,
            expected_generation=1,
        )
        after_closure = build_plan(connection)
        assert after_closure.plan_digest != after_policy.plan_digest
        assert after_closure.closure_revision_digest != after_policy.closure_revision_digest

        rs.record_hold(
            connection,
            command_id="hold-2",
            case_token=CASE,
            hold_id="hold-financial",
            scope=DataScope.FINANCIAL_PROVIDER_DATA,
            source_ref="hold-source-2",
            at=POLICY_AT,
        )
        after_hold = build_plan(connection)
        assert after_hold.plan_digest != after_closure.plan_digest
        assert after_hold.hold_revisions_digest != after_closure.hold_revisions_digest

        connection.execute(
            "UPDATE pb_workspaces SET payload_json = ?, revision = revision + 1 "
            "WHERE workspace_token = ?",
            (json.dumps({"story": "changed"}), WORKSPACE),
        )
        after_target = build_plan(connection)
        assert after_target.plan_digest != after_hold.plan_digest
        assert after_target.binding_digest != after_hold.binding_digest

        connection.execute(
            "INSERT INTO pb_workspace_history VALUES (?, ?, ?, ?)",
            (WORKSPACE, 1, json.dumps({"new": "revision"}), "2024"),
        )
        after_revision = build_plan(connection)
        assert after_revision.plan_digest != after_target.plan_digest
    finally:
        connection.close()


def test_proposed_policy_active_hold_and_missing_blockers_refuse(tmp_path: Path) -> None:
    proposed = make_database(
        tmp_path / "proposed.sqlite",
        adoption=PolicyAdoption.PROPOSED,
        case_workspace=True,
    )
    try:
        plan = build_plan(proposed)
        assert plan.summary.eligible is False
        assert "policy_not_adopted" in plan.summary.blocked_reasons
    finally:
        proposed.close()

    held = make_database(tmp_path / "held.sqlite", service_hold=True, case_workspace=True)
    try:
        plan = build_plan(held)
        assert plan.summary.eligible is False
        assert "preservation_hold_active" in plan.summary.blocked_reasons
    finally:
        held.close()

    missing = make_database(tmp_path / "missing.sqlite", case_workspace=True)
    try:
        plan = plan_service_data_retention(
            missing,
            case_token=CASE,
            evaluated_at=EVALUATED,
            blockers=None,
        )
        assert plan.summary.eligible is False
        assert "input_invalid" in plan.summary.blocked_reasons
    finally:
        missing.close()


def test_uncertain_money_and_original_mail_refuse(tmp_path: Path) -> None:
    connection = make_database(tmp_path / "parkingbreaker.sqlite", case_workspace=True)
    try:
        plan = build_plan(
            connection,
            blockers=UnresolvedBlockers(money=True, original_mail=True),
        )
        assert plan.summary.eligible is False
        assert "money_unresolved" in plan.summary.blocked_reasons
        assert "original_mail_unresolved" in plan.summary.blocked_reasons
    finally:
        connection.close()


def test_shared_owner_blob_is_preserved_and_other_owner_is_separate(tmp_path: Path) -> None:
    store = _FakeStore({(OWNER, PHOTO_REF): PHOTO_BYTES, (OTHER_OWNER, PHOTO_REF): PHOTO_BYTES})
    shared = make_database(
        tmp_path / "shared.sqlite", same_owner_reference=True, case_workspace=True
    )
    try:
        plan = build_plan(shared, blob_store=store)
        assert blob_disposition(plan, PHOTO_REF) is BlobDisposition.RETAINED_SHARED
    finally:
        shared.close()

    separate = make_database(
        tmp_path / "separate.sqlite", other_owner_reference=True, case_workspace=True
    )
    try:
        plan = build_plan(separate, blob_store=store)
        assert blob_disposition(plan, PHOTO_REF) is BlobDisposition.CANDIDATE
        assert all(blob.owner_dir == plan.owner_dir for blob in plan.blobs)
    finally:
        separate.close()


def test_orphan_and_missing_blobs_are_never_candidates(tmp_path: Path) -> None:
    orphan_ref = "photo_" + hashlib.sha256(b"orphan").hexdigest()
    store = _FakeStore({(OWNER, orphan_ref): b"orphan"})
    connection = make_database(tmp_path / "parkingbreaker.sqlite", case_workspace=True)
    try:
        plan = build_plan(connection, blob_store=store)
        assert blob_disposition(plan, PHOTO_REF) is BlobDisposition.MISSING_OR_ORPHAN
        assert all(blob.ref != orphan_ref for blob in plan.blobs)
        assert all(blob.disposition is not BlobDisposition.CANDIDATE for blob in plan.blobs)
    finally:
        connection.close()


def test_unknown_schema_protects_blobs_and_blocks_the_plan(tmp_path: Path) -> None:
    store = _FakeStore({(OWNER, PHOTO_REF): PHOTO_BYTES})
    connection = make_database(
        tmp_path / "parkingbreaker.sqlite", unknown_table=True, case_workspace=True
    )
    try:
        plan = build_plan(connection, blob_store=store)
        assert plan.summary.eligible is False
        assert PlanBlockedReason.UNKNOWN_SCHEMA_PRESENT.value in plan.summary.blocked_reasons
        assert blob_disposition(plan, PHOTO_REF) is BlobDisposition.PROTECTED_UNKNOWN_SCOPE
        assert plan.summary.unknown_table_count == 1
    finally:
        connection.close()


def test_missing_and_malformed_sources_fail_closed(tmp_path: Path) -> None:
    missing = make_database(tmp_path / "missing.sqlite", case_workspace=True)
    try:
        missing.execute("DROP TABLE pb_intake_drafts")
        plan = build_plan(missing)
        assert plan.summary.eligible is False
        assert PlanBlockedReason.SOURCE_TABLE_MISSING.value in plan.summary.blocked_reasons
    finally:
        missing.close()

    malformed = make_database(tmp_path / "malformed.sqlite", case_workspace=True)
    try:
        malformed.execute("ALTER TABLE pb_address_revisions ADD COLUMN extra TEXT")
        plan = build_plan(malformed)
        assert plan.summary.eligible is False
        assert PlanBlockedReason.SOURCE_SCHEMA_MALFORMED.value in plan.summary.blocked_reasons
    finally:
        malformed.close()


def test_plan_bound_is_explicitly_partial(tmp_path: Path) -> None:
    connection = make_database(tmp_path / "parkingbreaker.sqlite", history=40, case_workspace=True)
    try:
        plan = build_plan(connection, max_plan_rows=5)
        assert plan.partial is True
        assert plan.summary.partial is True
        assert plan.summary.eligible is False
        assert PlanBlockedReason.PLAN_BOUND_EXCEEDED.value in plan.summary.blocked_reasons
        assert plan.summary.target_count == 5
    finally:
        connection.close()


def test_caller_transaction_is_refused_without_committing_work(tmp_path: Path) -> None:
    connection = make_database(tmp_path / "parkingbreaker.sqlite", case_workspace=True)
    try:
        connection.execute("BEGIN")
        with pytest.raises(RetentionPlanRefused, match="caller_transaction_active"):
            build_plan(connection)
        assert connection.in_transaction is True
        connection.execute("ROLLBACK")
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_read_only_plan_leaves_database_bytes_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "parkingbreaker.sqlite"
    connection = make_database(path, case_workspace=True)
    try:
        before = path.read_bytes()
        sidecars_before = {entry.name for entry in tmp_path.iterdir()}
        build_plan(connection)
        assert path.read_bytes() == before
        assert {entry.name for entry in tmp_path.iterdir()} == sidecars_before
    finally:
        connection.close()


def test_raw_customer_markers_never_reach_repr_summary_or_errors(tmp_path: Path) -> None:
    connection = make_database(tmp_path / "parkingbreaker.sqlite", case_workspace=True)
    try:
        plan = build_plan(connection)
        summary_text = json.dumps(plan.summary.to_dict())
        assert SENTINEL not in repr(plan)
        assert SENTINEL not in summary_text
        assert CONTACT_SENTINEL not in repr(plan)
        assert CONTACT_SENTINEL not in summary_text
        assert SENTINEL not in repr(plan.targets)
        assert SENTINEL not in repr(plan.blobs)
        with pytest.raises(RetentionPlanRefused, match="invalid_case_token") as caught:
            plan_service_data_retention(
                connection,
                case_token="bad\n" + SENTINEL,
                evaluated_at=EVALUATED,
                blockers=UnresolvedBlockers(),
            )
        assert SENTINEL not in str(caught.value)
    finally:
        connection.close()


def test_sql_like_case_token_is_parameterised_and_refuses(tmp_path: Path) -> None:
    connection = make_database(tmp_path / "parkingbreaker.sqlite", case_workspace=True)
    try:
        plan = plan_service_data_retention(
            connection,
            case_token="case-1' OR 1=1 --",
            evaluated_at=EVALUATED,
            blockers=UnresolvedBlockers(),
        )
        assert plan.summary.eligible is False
        assert PlanBlockedReason.BINDING_MISSING.value in plan.summary.blocked_reasons
        assert connection.execute("SELECT COUNT(*) FROM pb_workspaces").fetchone() == (1,)
    finally:
        connection.close()


def test_restricted_originals_are_retained_under_a_separate_schedule(tmp_path: Path) -> None:
    connection = make_database(tmp_path / "parkingbreaker.sqlite", case_workspace=True)
    try:
        connection.execute(
            "CREATE TABLE pb_mail_attempts (case_token TEXT NOT NULL, attempt INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO pb_mail_attempts VALUES (?, ?)", (CASE, 1))
        plan = build_plan(connection)
        scopes = {
            scope: (present, row_count)
            for scope, present, row_count in plan.summary.retained_scopes
        }
        assert scopes["original_mail_attempts"] == (True, 1)
        assert "restricted_originals_retained" in plan.summary.outstanding_scopes
        assert plan.summary.service_purge_complete is False
    finally:
        connection.close()


def test_binding_requires_the_authoritative_case_workspace_column(tmp_path: Path) -> None:
    # The minimal fixture (still used by the frozen control) has no authoritative workspace
    # column, so the binding must fail closed instead of trusting the intake row alone.
    connection = make_database(tmp_path / "minimal.sqlite")
    try:
        unbound = build_plan(connection)
        assert unbound.summary.eligible is False
        assert unbound.workspace_token is None
        assert unbound.intake_id is None
        assert PlanBlockedReason.BINDING_INCONSISTENT.value in unbound.summary.blocked_reasons

        connection.execute("ALTER TABLE pb_cases ADD COLUMN workspace_token TEXT")
        connection.execute(
            "UPDATE pb_cases SET workspace_token=? WHERE case_token=?", (WORKSPACE, CASE)
        )
        bound = build_plan(connection)
        assert bound.summary.eligible is True
        assert bound.workspace_token == WORKSPACE
        assert bound.intake_id == INTAKE
        assert bound.binding_digest is not None
    finally:
        connection.close()


def test_case_intake_workspace_and_existing_case_must_all_agree(tmp_path: Path) -> None:
    mismatch_case = make_database(tmp_path / "case.sqlite", case_workspace=True)
    try:
        mismatch_case.execute(
            "UPDATE pb_cases SET workspace_token=? WHERE case_token=?",
            ("workspace-elsewhere", CASE),
        )
        plan = build_plan(mismatch_case)
        assert plan.summary.eligible is False
        assert plan.workspace_token is None
        assert any("binding" in reason for reason in plan.summary.blocked_reasons)
    finally:
        mismatch_case.close()

    mismatch_intake = make_database(tmp_path / "intake.sqlite", case_workspace=True)
    try:
        mismatch_intake.execute(
            "UPDATE pb_intake_records SET workspace_token=? WHERE case_token=?",
            ("workspace-elsewhere", CASE),
        )
        plan = build_plan(mismatch_intake)
        assert plan.summary.eligible is False
        assert plan.workspace_token is None
        assert PlanBlockedReason.BINDING_INCONSISTENT.value in plan.summary.blocked_reasons
    finally:
        mismatch_intake.close()

    missing_intake = make_database(tmp_path / "no-intake.sqlite", case_workspace=True)
    try:
        missing_intake.execute("DELETE FROM pb_intake_records WHERE case_token=?", (CASE,))
        plan = build_plan(missing_intake)
        assert plan.summary.eligible is False
        assert PlanBlockedReason.BINDING_MISSING.value in plan.summary.blocked_reasons
    finally:
        missing_intake.close()

    missing_case = make_database(tmp_path / "no-case.sqlite", case_workspace=True)
    try:
        missing_case.execute("DELETE FROM pb_cases WHERE case_token=?", (CASE,))
        plan = build_plan(missing_case)
        assert plan.summary.eligible is False
        assert PlanBlockedReason.BINDING_MISSING.value in plan.summary.blocked_reasons
    finally:
        missing_case.close()


def test_ambiguous_multiple_intakes_for_one_case_fail_closed(tmp_path: Path) -> None:
    connection = make_database(tmp_path / "ambiguous.sqlite", case_workspace=True)
    try:
        connection.execute("DROP TABLE pb_intake_records")
        connection.execute(
            """CREATE TABLE pb_intake_records (
                intake_id TEXT PRIMARY KEY,
                workspace_token TEXT NOT NULL,
                intake_json TEXT NOT NULL,
                prehold_json TEXT NOT NULL,
                agency_json TEXT,
                selected_tier TEXT,
                case_token TEXT,
                created_at TEXT NOT NULL,
                revision INTEGER NOT NULL
            )"""
        )
        for intake_id in (INTAKE, "intake-2"):
            connection.execute(
                "INSERT INTO pb_intake_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intake_id,
                    WORKSPACE,
                    json.dumps({"story": SENTINEL}),
                    json.dumps({"prehold": SENTINEL}),
                    None,
                    "STANDARD",
                    CASE,
                    "2024",
                    1,
                ),
            )
        plan = build_plan(connection)
        assert plan.summary.eligible is False
        assert plan.workspace_token is None
        assert PlanBlockedReason.BINDING_AMBIGUOUS.value in plan.summary.blocked_reasons
    finally:
        connection.close()


def test_source_fingerprint_binds_table_trigger_and_index_definitions(tmp_path: Path) -> None:
    path = tmp_path / "triggers.sqlite"
    connection = make_database(path, case_workspace=True)
    try:
        baseline = build_plan(connection)
        before = path.read_bytes()
        assert build_plan(connection).plan_digest == baseline.plan_digest
        assert path.read_bytes() == before

        connection.execute(
            "CREATE INDEX pb_workspaces_payload_index ON pb_workspaces (payload_json)"
        )
        with_index = build_plan(connection)
        assert with_index.source_fingerprint != baseline.source_fingerprint
        assert with_index.plan_digest != baseline.plan_digest
        assert with_index.summary.eligible is True

        connection.execute(
            "CREATE TRIGGER pb_workspaces_guard BEFORE UPDATE ON pb_workspaces "
            "BEGIN SELECT RAISE(ABORT, 'fixed'); END"
        )
        guarded = build_plan(connection)
        assert guarded.source_fingerprint != with_index.source_fingerprint
        assert guarded.plan_digest != with_index.plan_digest
        assert guarded.summary.eligible is True
        assert build_plan(connection).plan_digest == guarded.plan_digest

        connection.execute("DROP TRIGGER pb_workspaces_guard")
        dropped = build_plan(connection)
        assert dropped.plan_digest != guarded.plan_digest
        assert dropped.source_fingerprint == with_index.source_fingerprint

        connection.execute(
            "CREATE TRIGGER pb_workspaces_guard BEFORE UPDATE ON pb_workspaces "
            "BEGIN SELECT RAISE(ABORT, 'fixed'); END"
        )
        assert build_plan(connection).plan_digest == guarded.plan_digest

        connection.execute("DROP TRIGGER pb_workspaces_guard")
        connection.execute(
            "CREATE TRIGGER pb_workspaces_guard BEFORE UPDATE ON pb_workspaces "
            "BEGIN SELECT RAISE(ABORT, 'changed'); END"
        )
        changed = build_plan(connection)
        assert changed.plan_digest != guarded.plan_digest

        connection.execute("DROP TRIGGER pb_workspaces_guard")
        connection.execute("DROP INDEX pb_workspaces_payload_index")
        connection.execute("CREATE INDEX pb_workspaces_payload_index ON pb_workspaces (updated_at)")
        changed_index = build_plan(connection)
        assert changed_index.source_fingerprint != changed.source_fingerprint
        assert changed_index.plan_digest != changed.plan_digest

        before_plan = path.read_bytes()
        build_plan(connection)
        assert path.read_bytes() == before_plan
    finally:
        connection.close()


def test_malformed_schema_object_sql_fails_closed(tmp_path: Path) -> None:
    connection = make_database(tmp_path / "malformed-trigger.sqlite", case_workspace=True)
    try:
        connection.execute(
            "CREATE TRIGGER pb_workspaces_guard BEFORE UPDATE ON pb_workspaces "
            "BEGIN SELECT RAISE(ABORT, 'fixed'); END"
        )
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=NULL WHERE type='trigger' AND name='pb_workspaces_guard'"
        )
        connection.execute("PRAGMA writable_schema=OFF")
        plan = build_plan(connection)
        assert plan.summary.eligible is False
        assert PlanBlockedReason.SOURCE_SCHEMA_MALFORMED.value in plan.summary.blocked_reasons
    finally:
        connection.close()


def test_real_sqlite_transaction_store_schema_binds_the_case(tmp_path: Path) -> None:
    # Integration with the production store schema: the authoritative pb_cases.workspace_token
    # column, real triggers and indexes must be recognised. The store does not create the two
    # immutable target tables at all, so the plan stays blocked for missing sources; this proves
    # binding recognition only, not production completeness.
    path = tmp_path / "real-store.sqlite"
    with SqliteTransactionStore(path) as store:
        store.create_owned_workspace(OWNER, WORKSPACE, Workspace(notes="private"), POLICY_AT)
        database = store._require_open()
        case = store.start_case(
            WORKSPACE, "authorization-1", POLICY_AT, POLICY_AT + timedelta(days=1)
        )
        database.execute(
            "INSERT INTO pb_intake_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                INTAKE,
                WORKSPACE,
                json.dumps({"story": SENTINEL}),
                json.dumps({"prehold": SENTINEL}),
                None,
                "STANDARD",
                case.case_token,
                "2024-01-01T00:00:00+00:00",
                1,
            ),
        )
        rs.initialize_retention_state(database)
        rs.record_policy(
            database,
            command_id="policy-1",
            adoption=PolicyAdoption.ADOPTED,
            timezone=ZoneInfo("UTC"),
            at=POLICY_AT,
            adoption_evidence_ref="evidence-1",
        )
        rs.record_closure(
            database,
            command_id="closure-1",
            case_token=case.case_token,
            kind=ClosureKind.CLOSED,
            anchor_at=ANCHOR,
            source_ref="closure-source-1",
            at=POLICY_AT,
        )
        plan = plan_service_data_retention(
            database,
            case_token=case.case_token,
            evaluated_at=EVALUATED,
            blockers=UnresolvedBlockers(),
        )
        statuses = {status.table: status.state for status in plan.source_statuses}
        assert plan.workspace_token == WORKSPACE
        assert plan.intake_id == INTAKE
        assert plan.binding_digest is not None
        for table in ("pb_cases", "pb_workspaces", "pb_intake_records", "pb_intake_owners"):
            assert statuses[table] is SourceState.PRESENT
        assert not any("binding" in reason for reason in plan.summary.blocked_reasons)
        assert plan.summary.eligible is False
        assert PlanBlockedReason.SOURCE_TABLE_MISSING.value in plan.summary.blocked_reasons
        assert plan.summary.unknown_table_count == 0
        assert plan.summary.source_malformed_count == 0
        again = plan_service_data_retention(
            database,
            case_token=case.case_token,
            evaluated_at=EVALUATED,
            blockers=UnresolvedBlockers(),
        )
        assert again.plan_digest == plan.plan_digest
