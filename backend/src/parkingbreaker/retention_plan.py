"""Read-only one-case service-data retention dry-run plan.

This module is a planning primitive, not a purge engine. Given an existing authority SQLite
connection and one explicit ``case_token``, it resolves the case's workspace, intake and
owner, loads the stored policy/closure/hold revisions and the caller-supplied unresolved
blockers, inventories the known ordinary service-copy targets, classifies canonical photo
blob references conservatively, and returns a hash-bound private manifest plus a content-free
summary. It never writes, migrates, deletes, restores, schedules, adopts a policy, removes an
owner directory or contacts a provider, and it opens no database of its own.

The plan digest binds a later execution to the exact source schema, component versions, case
binding, policy/closure/hold revisions, blockers and target payload digests that were
observed here. It is a change-detection binding, not a statement that the observed data is
true, complete or lawful, and it never authorizes deletion. ``service_purge_complete`` stays
``False`` because nothing is executed and backups, frozen PDF exports, WAL/free pages and
provider-held copies remain outstanding scopes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Final, Protocol

from parkingbreaker.retention_policy import (
    DataScope,
    RetentionDecision,
    UnresolvedBlockers,
    evaluate_service_retention,
)
from parkingbreaker.retention_state import (
    CaseRetentionSnapshot,
    RetentionStateCorrupted,
    RetentionStateInputError,
    RetentionStateRefusal,
    RetentionTransactionRefused,
    case_snapshot,
)

COMPONENT: Final = "retention_plan"
FORMAT_VERSION: Final = 1
DEFAULT_BATCH_SIZE: Final = 200
MAX_BATCH_SIZE: Final = 500
DEFAULT_MAX_PLAN_ROWS: Final = 20_000

_PHOTO_REF: Final = re.compile(r"photo_[0-9a-f]{64}")
_MAX_CASE_TOKEN: Final = 256

OUTSTANDING_SCOPES: Final[tuple[str, ...]] = (
    "backups",
    "frozen_pdf_exports",
    "provider_held_copies",
    "sqlite_wal_and_free_pages",
    "restricted_originals_retained",
    "external_deletion_not_claimed",
)


class BlobStoreLike(Protocol):
    """The owner-aware read half of ``parkingbreaker.blob_store.BlobStore``."""

    def get(self, owner: str, ref: str) -> bytes | None: ...


class RetentionPlanRefusal(enum.StrEnum):
    """Stable refusal reasons for a caller error. Values carry no content or path."""

    INVALID_CASE_TOKEN = "invalid_case_token"
    INVALID_BATCH_SIZE = "invalid_batch_size"
    INVALID_PLAN_BOUND = "invalid_plan_bound"
    CALLER_TRANSACTION_ACTIVE = "caller_transaction_active"
    SOURCE_UNAVAILABLE = "source_unavailable"


class RetentionPlanRefused(ValueError):
    """A caller-provided value or connection state cannot be accepted."""

    def __init__(self, reason: RetentionPlanRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


class PlanBlockedReason(enum.StrEnum):
    """Fixed fail-closed reasons. Values never contain stored content or paths."""

    BINDING_MISSING = "binding_missing"
    BINDING_AMBIGUOUS = "binding_ambiguous"
    BINDING_INCONSISTENT = "binding_inconsistent"
    SOURCE_TABLE_MISSING = "source_table_missing"
    SOURCE_SCHEMA_MALFORMED = "source_schema_malformed"
    UNKNOWN_SCHEMA_PRESENT = "unknown_schema_present"
    ROW_MALFORMED = "row_malformed"
    RETENTION_STATE_UNAVAILABLE = "retention_state_unavailable"
    UNKNOWN_CASE = "unknown_case"
    PLAN_BOUND_EXCEEDED = "plan_bound_exceeded"


class BlobReason(enum.StrEnum):
    """Fixed conservative blob classifications. Values never contain a ref or owner."""

    SHARED_REFERENCE = "shared_reference"
    UNKNOWN_SCHEMA = "unknown_schema"
    SOURCE_UNAVAILABLE = "source_unavailable"
    PRESENCE_UNKNOWN = "presence_unknown"
    MISSING_BYTES = "missing_bytes"
    DIGEST_MISMATCH = "digest_mismatch"
    UNREADABLE = "unreadable"


class SourceState(enum.StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    MALFORMED = "malformed"


class TargetCategory(enum.StrEnum):
    """One known ordinary service-copy target."""

    WORKSPACE_PAYLOAD = "workspace_payload"
    WORKSPACE_HISTORY = "workspace_history"
    INTAKE_RECORD = "intake_record"
    INTAKE_DRAFT = "intake_draft"
    INTAKE_RESEARCH = "intake_research"
    DRAFT_ATTEMPT = "draft_attempt"
    BETA_FEEDBACK_NOTICE = "beta_feedback_notice"
    TICKET_INTAKE_OPERATION = "ticket_intake_operation"
    BETA_CONTACT_TARGET = "beta_contact_target"
    ADDRESS_REVISION = "address_revision"
    ADDRESS_CONSENT = "address_consent"
    ADDRESS_DECISION = "address_decision"
    REQUIRED_COPIES = "required_copies"
    APPROVAL_MANIFEST = "approval_manifest"


class TargetDisposition(enum.StrEnum):
    CANDIDATE = "candidate"
    RETAINED_RESTRICTED = "retained_restricted"
    REQUIRES_MIGRATION = "requires_migration"
    PROTECTED_SHARED = "protected_shared"
    PROTECTED_UNKNOWN = "protected_unknown"
    PROTECTED_UNAVAILABLE = "protected_unavailable"
    PROTECTED_MALFORMED = "protected_malformed"


class BlobDisposition(enum.StrEnum):
    CANDIDATE = "candidate"
    RETAINED_SHARED = "retained_shared"
    PROTECTED_UNKNOWN_SCOPE = "protected_unknown_scope"
    PROTECTED_PRESENCE_UNKNOWN = "protected_presence_unknown"
    MISSING_OR_ORPHAN = "missing_or_orphan"
    PROTECTED_DIGEST_MISMATCH = "protected_digest_mismatch"
    PROTECTED_UNREADABLE = "protected_unreadable"


# Exact current column/type layouts for every table the plan reads as a source target.
_EXACT_SCHEMA: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "pb_workspaces": (
        ("workspace_token", "TEXT"),
        ("payload_json", "TEXT"),
        ("updated_at", "TEXT"),
        ("revision", "INTEGER"),
    ),
    "pb_workspace_history": (
        ("workspace_token", "TEXT"),
        ("revision", "INTEGER"),
        ("payload_json", "TEXT"),
        ("updated_at", "TEXT"),
    ),
    "pb_intake_owners": (
        ("workspace_token", "TEXT"),
        ("owner", "TEXT"),
        ("created_at", "TEXT"),
    ),
    "pb_intake_records": (
        ("intake_id", "TEXT"),
        ("workspace_token", "TEXT"),
        ("intake_json", "TEXT"),
        ("prehold_json", "TEXT"),
        ("agency_json", "TEXT"),
        ("selected_tier", "TEXT"),
        ("case_token", "TEXT"),
        ("created_at", "TEXT"),
        ("revision", "INTEGER"),
    ),
    "pb_intake_drafts": (
        ("intake_id", "TEXT"),
        ("letter_text", "TEXT"),
        ("spans_json", "TEXT"),
        ("flags_json", "TEXT"),
        ("adopted_sha256", "TEXT"),
        ("adopted_at", "TEXT"),
        ("adopted_by", "TEXT"),
        ("packet_sha256", "TEXT"),
        ("updated_at", "TEXT"),
        ("revision", "INTEGER"),
    ),
    "pb_intake_research": (
        ("intake_id", "TEXT"),
        ("research_json", "TEXT"),
        ("answers_json", "TEXT"),
        ("updated_at", "TEXT"),
        ("revision", "INTEGER"),
    ),
    "pb_draft_attempts": (
        ("case_token", "TEXT"),
        ("generation", "INTEGER"),
        ("attempt", "INTEGER"),
        ("inputs_sha256", "TEXT"),
        ("started_at", "TEXT"),
        ("outcome", "TEXT"),
        ("result_json", "TEXT"),
    ),
    "pb_beta_feedback_notices": (
        ("sequence", "INTEGER"),
        ("case_id", "TEXT"),
        ("owner", "TEXT"),
        ("notice_id", "TEXT"),
        ("request_id", "TEXT"),
        ("received_at", "TEXT"),
        ("payload_json", "TEXT"),
        ("payload_sha256", "TEXT"),
        ("correction_of", "TEXT"),
        ("needs_confirmation", "INTEGER"),
        ("review_state", "TEXT"),
    ),
    "pb_ticket_intake_operations": (
        ("request_id", "TEXT"),
        ("intake_id", "TEXT"),
        ("workspace_token", "TEXT"),
        ("owner", "TEXT"),
        ("operation", "TEXT"),
        ("binding_sha256", "TEXT"),
        ("claim_token_sha256", "TEXT"),
        ("status", "TEXT"),
        ("result_json", "TEXT"),
        ("usage_json", "TEXT"),
        ("claimed_at", "TEXT"),
        ("completed_at", "TEXT"),
    ),
    "pb_beta_contact_targets": (
        ("case_id", "TEXT"),
        ("owner", "TEXT"),
        ("case_token", "TEXT"),
        ("target_id", "TEXT"),
        ("revision", "INTEGER"),
        ("channel", "TEXT"),
        ("contact_value", "TEXT"),
        ("value_sha256", "TEXT"),
        ("disclosure_version", "INTEGER"),
        ("disclosure_sha256", "TEXT"),
        ("permitted", "INTEGER"),
        ("recorded_at", "TEXT"),
    ),
    "pb_address_revisions": (
        ("case_id", "TEXT"),
        ("owner", "TEXT"),
        ("revision", "INTEGER"),
        ("payload_json", "TEXT"),
    ),
    "pb_address_consents": (
        ("case_id", "TEXT"),
        ("owner", "TEXT"),
        ("revision", "INTEGER"),
        ("payload_json", "TEXT"),
    ),
    "pb_address_decisions": (
        ("case_id", "TEXT"),
        ("owner", "TEXT"),
        ("revision", "INTEGER"),
        ("payload_json", "TEXT"),
    ),
    "pb_required_copies": (
        ("case_id", "TEXT"),
        ("owner", "TEXT"),
        ("research_sha256", "TEXT"),
        ("selections_json", "TEXT"),
        ("revision", "INTEGER"),
    ),
}

# The immutable rows that need a versioned payload-to-NULL migration and decoder later.
_REQUIRES_MIGRATION: Final[frozenset[str]] = frozenset(
    {
        "pb_draft_attempts",
        "pb_beta_feedback_notices",
        "pb_ticket_intake_operations",
        "pb_beta_contact_targets",
    }
)

# Every table this code base is known to create. Anything else is unknown schema.
_KNOWN_TABLES: Final[frozenset[str]] = frozenset(
    {
        "pb_access_email_calls",
        "pb_address_consents",
        "pb_address_decisions",
        "pb_address_locks",
        "pb_address_revisions",
        "pb_ai_calls",
        "pb_ai_journal",
        "pb_ai_spend",
        "pb_approval_manifests",
        "pb_beta_batches",
        "pb_beta_campaigns",
        "pb_beta_candidates",
        "pb_beta_contact_commands",
        "pb_beta_contact_evidence",
        "pb_beta_contact_replies",
        "pb_beta_contact_targets",
        "pb_beta_entitlements",
        "pb_beta_feedback_notices",
        "pb_beta_jurisdiction_aliases",
        "pb_case_history",
        "pb_case_hold_busy",
        "pb_case_hold_commands",
        "pb_case_holds",
        "pb_case_ops_ids",
        "pb_cases",
        "pb_coverage_counters",
        "pb_draft_attempts",
        "pb_draft_runs",
        "pb_draft_slots",
        "pb_identity_accounts",
        "pb_identity_links",
        "pb_identity_sessions",
        "pb_incidents",
        "pb_intake_authorization_attempts",
        "pb_intake_drafts",
        "pb_intake_owners",
        "pb_intake_records",
        "pb_intake_research",
        "pb_journey_events",
        "pb_journey_links",
        "pb_journey_capture",
        "pb_journey_event_context",
        "pb_mail_attempts",
        "pb_mail_destination_bindings",
        "pb_notices",
        "pb_notification_access_links",
        "pb_overseer_key_versions",
        "pb_payment_cursor",
        "pb_payment_operations",
        "pb_pending_effects",
        "pb_price_credentials",
        "pb_price_scopes",
        "pb_price_visitors",
        "pb_required_copies",
        "pb_retention_case_closures",
        "pb_retention_closure_commands",
        "pb_retention_hold_commands",
        "pb_retention_holds",
        "pb_retention_policies",
        "pb_retention_policy_commands",
        "pb_return_remedies",
        "pb_review_resolution_expiries",
        "pb_review_resolutions",
        "pb_runtime_service_phase",
        "pb_setting_changes",
        "pb_setting_proposal_expiries",
        "pb_setting_proposal_outcomes",
        "pb_setting_proposals",
        "pb_store_versions",
        "pb_submission_cursors",
        "pb_submission_operations",
        "pb_support_codes",
        "pb_support_confirmation_emails",
        "pb_support_messages",
        "pb_sweep_runs",
        "pb_ticket_intake_confirmations",
        "pb_ticket_intake_operation_aliases",
        "pb_ticket_intake_operations",
        "pb_ticket_intake_reservations",
        "pb_ticket_ocr_observations",
        "pb_ticket_review_observations",
        "pb_ticket_review_sessions",
        "pb_tracking_email_mark",
        "pb_tracking_emails",
        "pb_trick_sightings",
        "pb_webhook_backlog",
        "pb_webhook_final_events",
        "pb_webhook_unmatched",
        "pb_webhooks",
        "pb_workspace_history",
        "pb_workspaces",
    }
)

# Restricted originals and shared evidence retained under a separate schedule. The optional
# column scopes the count to this case; ``None`` means the scope is shared and counted whole.
_RESTRICTED_SCOPES: Final[tuple[tuple[str, str, str | None], ...]] = (
    ("original_mail_attempts", "pb_mail_attempts", "case_token"),
    ("intake_authorization", "pb_intake_authorization_attempts", "intake_id"),
    ("submission_cursors", "pb_submission_cursors", "case_token"),
    ("submission_operations", "pb_submission_operations", "case_token"),
    ("payment_cursor", "pb_payment_cursor", "case_token"),
    ("payment_operations", "pb_payment_operations", "case_token"),
    ("webhook_events", "pb_webhooks", "case_token"),
    ("webhook_backlog", "pb_webhook_backlog", None),
    ("webhook_unmatched", "pb_webhook_unmatched", None),
    ("webhook_final_events", "pb_webhook_final_events", None),
    ("return_remedies", "pb_return_remedies", "case_token"),
    ("lifecycle_effects", "pb_pending_effects", "case_token"),
    ("tracking_email", "pb_tracking_emails", "case_token"),
    ("notification_access_links", "pb_notification_access_links", None),
    ("identity_accounts", "pb_identity_accounts", None),
    ("support_codes", "pb_support_codes", "case_token"),
    ("support_confirmation_emails", "pb_support_confirmation_emails", "case_token"),
    ("support_message_audit", "pb_support_messages", None),
    ("journey_links", "pb_journey_links", None),
    ("journey_capture", "pb_journey_capture", None),
    ("journey_event_context", "pb_journey_event_context", None),
    ("journey_events", "pb_journey_events", None),
    ("ticket_ocr_observations", "pb_ticket_ocr_observations", "intake_id"),
    ("ticket_review_sessions", "pb_ticket_review_sessions", None),
    ("ticket_review_observations", "pb_ticket_review_observations", None),
)


@dataclasses.dataclass(frozen=True, repr=False)
class SourceStatus:
    """One source table's observed state. The table name is a fixed schema identifier."""

    table: str
    state: SourceState

    def __repr__(self) -> str:
        return f"SourceStatus(table={self.table!r}, state={self.state.value!r})"


@dataclasses.dataclass(frozen=True, repr=False)
class PlanTarget:
    """One scoped opaque target row. Raw payload, contact and story text never appear."""

    category: TargetCategory
    table: str
    row_key: str
    column: str
    content_sha256: str | None
    content_bytes: int | None
    disposition: TargetDisposition
    reason: str | None

    def __repr__(self) -> str:
        return (
            f"PlanTarget(category={self.category.value!r}, "
            f"disposition={self.disposition.value!r}, reason={self.reason!r})"
        )


@dataclasses.dataclass(frozen=True, repr=False)
class BlobTarget:
    """One conservative photo-reference classification. The ref/owner dir are opaque."""

    owner_dir: str
    ref: str
    disposition: BlobDisposition
    reason: str | None
    reference_count: int

    def __repr__(self) -> str:
        return (
            f"BlobTarget(disposition={self.disposition.value!r}, "
            f"reason={self.reason!r}, references={self.reference_count})"
        )


@dataclasses.dataclass(frozen=True)
class RetainedScope:
    """One restricted-original scope, with a case-scoped or shared row count."""

    scope: str
    table: str
    present: bool
    row_count: int | None


@dataclasses.dataclass(frozen=True)
class RetentionPlanSummary:
    """Content-free summary: counts, fixed categories and fixed reasons only."""

    format_version: int
    eligible: bool
    blocked_reasons: tuple[str, ...]
    partial: bool
    source_present_count: int
    source_missing_count: int
    source_malformed_count: int
    unknown_table_count: int
    target_count: int
    target_counts: tuple[tuple[str, int], ...]
    disposition_counts: tuple[tuple[str, int], ...]
    blob_count: int
    blob_counts: tuple[tuple[str, int], ...]
    retained_scopes: tuple[tuple[str, bool, int | None], ...]
    outstanding_scopes: tuple[str, ...]
    service_purge_complete: bool = False
    deletion_authorized: bool = False
    retention_policy_activated: bool = False
    owner_directories_removed: bool = False

    def to_dict(self) -> dict[str, object]:
        """Serialise only aggregate counts and fixed identifiers."""

        return {
            "format_version": self.format_version,
            "eligible": self.eligible,
            "blocked_reasons": list(self.blocked_reasons),
            "partial": self.partial,
            "source_present_count": self.source_present_count,
            "source_missing_count": self.source_missing_count,
            "source_malformed_count": self.source_malformed_count,
            "unknown_table_count": self.unknown_table_count,
            "target_count": self.target_count,
            "target_counts": [
                {"category": category, "count": count} for category, count in self.target_counts
            ],
            "disposition_counts": [
                {"disposition": disposition, "count": count}
                for disposition, count in self.disposition_counts
            ],
            "blob_count": self.blob_count,
            "blob_counts": [
                {"disposition": disposition, "count": count}
                for disposition, count in self.blob_counts
            ],
            "retained_scopes": [
                {"scope": scope, "present": present, "row_count": row_count}
                for scope, present, row_count in self.retained_scopes
            ],
            "outstanding_scopes": list(self.outstanding_scopes),
            "service_purge_complete": self.service_purge_complete,
            "deletion_authorized": self.deletion_authorized,
            "retention_policy_activated": self.retention_policy_activated,
            "owner_directories_removed": self.owner_directories_removed,
        }


@dataclasses.dataclass(frozen=True, repr=False)
class RetentionPlan:
    """A private, hash-bound dry-run manifest. Nothing here has been executed."""

    format_version: int
    case_token: str
    workspace_token: str | None
    intake_id: str | None
    owner_dir: str | None
    plan_digest: str
    source_fingerprint: str
    binding_digest: str | None
    policy_revision_digest: str | None
    closure_revision_digest: str | None
    hold_revisions_digest: str | None
    decision_digest: str
    partial: bool
    source_statuses: tuple[SourceStatus, ...]
    targets: tuple[PlanTarget, ...]
    blobs: tuple[BlobTarget, ...]
    retained_scopes: tuple[RetainedScope, ...]
    summary: RetentionPlanSummary

    def __repr__(self) -> str:
        return (
            f"RetentionPlan(eligible={self.summary.eligible!r}, "
            f"blocked_reasons={self.summary.blocked_reasons!r}, "
            f"target_count={self.summary.target_count}, "
            f"blob_count={self.summary.blob_count}, partial={self.partial!r})"
        )


@dataclasses.dataclass(frozen=True)
class _Binding:
    case_token: str
    workspace_token: str
    intake_id: str
    owner: str
    workspace_revision: int
    intake_revision: int


class _PlanBoundReached(Exception):
    """The configured maximum-plan bound was reached; the plan is explicitly partial."""


class _PageMalformed(Exception):
    """A page could not advance; the inventory is explicitly partial, never silently short."""


def _refuse(reason: RetentionPlanRefusal) -> RetentionPlanRefused:
    return RetentionPlanRefused(reason)


def _check_case_token(value: object) -> str:
    if type(value) is not str or not value or len(value) > _MAX_CASE_TOKEN:
        raise _refuse(RetentionPlanRefusal.INVALID_CASE_TOKEN)
    if value != value.strip():
        raise _refuse(RetentionPlanRefusal.INVALID_CASE_TOKEN)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise _refuse(RetentionPlanRefusal.INVALID_CASE_TOKEN)
    return value


def _check_batch_size(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_BATCH_SIZE:
        raise _refuse(RetentionPlanRefusal.INVALID_BATCH_SIZE)
    return value


def _check_plan_bound(value: object) -> int:
    if type(value) is not int or value < 1:
        raise _refuse(RetentionPlanRefusal.INVALID_PLAN_BOUND)
    return value


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _timestamp_text(value: object) -> str | None:
    if not isinstance(value, datetime):
        return None
    try:
        if value.utcoffset() is not None:
            return value.astimezone(UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return None
    return value.isoformat()


def _normalised_sql(value: object) -> str | None:
    if type(value) is not str:
        return None
    return " ".join(value.split())


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_info(connection: sqlite3.Connection, table: str) -> tuple[tuple[str, str], ...]:
    if table not in _KNOWN_TABLES:
        raise AssertionError("unexpected table")
    rows = connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    return tuple((str(row[1]), str(row[2]).upper()) for row in rows)


def _schema_snapshot(
    connection: sqlite3.Connection,
) -> tuple[
    dict[str, SourceState],
    dict[str, str],
    tuple[str, ...],
    tuple[tuple[str, int | None], ...],
    tuple[tuple[str, str, str, str | None], ...],
    bool,
]:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'view', 'trigger', 'index')"
    ).fetchall()
    kinds: dict[str, str] = {}
    schema_objects: list[tuple[str, str, str, str | None]] = []
    malformed = False
    for row in rows:
        kind, name, tbl_name, sql = row[0], row[1], row[2], row[3]
        if not isinstance(kind, str) or not isinstance(name, str) or not name:
            malformed = True
            continue
        if name.startswith("sqlite_"):
            continue
        if not isinstance(tbl_name, str) or not tbl_name:
            malformed = True
            tbl_name = ""
        if kind in ("table", "view"):
            kinds[name] = kind
        normalised_sql = _normalised_sql(sql)
        if not normalised_sql:
            malformed = True
        schema_objects.append((kind, name, tbl_name, normalised_sql))
    for kind, _name, tbl_name, _sql in schema_objects:
        if kind in ("trigger", "index") and tbl_name not in kinds:
            malformed = True

    states: dict[str, SourceState] = {}
    for table, expected in _EXACT_SCHEMA.items():
        if kinds.get(table) != "table":
            states[table] = SourceState.MISSING if table not in kinds else SourceState.MALFORMED
            continue
        states[table] = (
            SourceState.PRESENT
            if _table_info(connection, table) == expected
            else SourceState.MALFORMED
        )

    # Older stores may predate approval manifests. A present table is never trusted
    # solely because its name is in the known-table registry.
    approval_table = "pb_approval_manifests"
    if approval_table in kinds:
        expected_approval_columns = (
            ("case_token", "TEXT"),
            ("attempt", "INTEGER"),
            ("case_id", "TEXT"),
            ("approved_at_utc", "TEXT"),
            ("build_commit", "TEXT"),
            ("raw_customer_text_sha256", "TEXT"),
            ("approved_text_sha256", "TEXT"),
            ("packet_pdf_sha256", "TEXT"),
            ("envelope_sha256", "TEXT"),
            ("affirmations_json", "TEXT"),
            ("affirmations_sha256", "TEXT"),
            ("manifest_sha256", "TEXT"),
            ("provider_letter_id", "TEXT"),
            ("expected_delivery", "TEXT"),
            ("dispatched_pdf_sha256", "TEXT"),
            ("dispatched_at_utc", "TEXT"),
        )
        states[approval_table] = (
            SourceState.PRESENT
            if kinds[approval_table] == "table"
            and _table_info(connection, approval_table) == expected_approval_columns
            else SourceState.MALFORMED
        )

    for table in ("pb_cases", "pb_store_versions"):
        if kinds.get(table) != "table":
            states[table] = SourceState.MISSING if table not in kinds else SourceState.MALFORMED
        elif table == "pb_cases":
            columns = _table_info(connection, table)
            authoritative = ("case_token", "TEXT") in columns and (
                "workspace_token",
                "TEXT",
            ) in columns
            states[table] = SourceState.PRESENT if authoritative else SourceState.MALFORMED
        else:
            states[table] = (
                SourceState.PRESENT
                if _table_info(connection, table) == (("component", "TEXT"), ("version", "INTEGER"))
                else SourceState.MALFORMED
            )

    unknown = tuple(sorted(name for name in kinds if name not in _KNOWN_TABLES))
    components: tuple[tuple[str, int | None], ...] = ()
    if states.get("pb_store_versions") is SourceState.PRESENT:
        try:
            component_rows = connection.execute(
                "SELECT component, version FROM pb_store_versions ORDER BY component"
            ).fetchall()
        except sqlite3.DatabaseError:
            components = ()
        else:
            components = tuple(
                (
                    row[0] if isinstance(row[0], str) else "malformed",
                    row[1] if type(row[1]) is int else None,
                )
                for row in component_rows
            )
    return states, kinds, unknown, components, tuple(schema_objects), malformed


def _source_fingerprint(
    schema_objects: tuple[tuple[str, str, str, str | None], ...],
    unknown: tuple[str, ...],
    components: tuple[tuple[str, int | None], ...],
) -> str:
    return _digest(
        {
            "format_version": FORMAT_VERSION,
            "objects": [
                [kind, name, tbl_name, sql]
                for kind, name, tbl_name, sql in sorted(
                    schema_objects, key=lambda item: (item[0], item[1])
                )
            ],
            "unknown_tables": list(unknown),
            "components": [[component, version] for component, version in components],
        }
    )


def _resolve_binding(
    connection: sqlite3.Connection, case_token: str, states: dict[str, SourceState]
) -> tuple[_Binding | None, PlanBlockedReason | None]:
    required = ("pb_cases", "pb_intake_records", "pb_intake_owners", "pb_workspaces")
    for table in required:
        state = states.get(table)
        if state is SourceState.MISSING:
            return None, PlanBlockedReason.BINDING_MISSING
        if state is not SourceState.PRESENT:
            return None, PlanBlockedReason.BINDING_INCONSISTENT
    try:
        rows = connection.execute(
            "SELECT c.workspace_token, r.intake_id, r.workspace_token, r.revision, "
            "w.workspace_token, w.revision, o.workspace_token, o.owner "
            "FROM pb_cases AS c "
            "JOIN pb_intake_records AS r ON r.case_token = c.case_token "
            "JOIN pb_workspaces AS w ON w.workspace_token = c.workspace_token "
            "JOIN pb_intake_owners AS o ON o.workspace_token = c.workspace_token "
            "WHERE c.case_token = ?",
            (case_token,),
        ).fetchall()
    except sqlite3.DatabaseError:
        return None, PlanBlockedReason.BINDING_INCONSISTENT
    if not rows:
        return None, PlanBlockedReason.BINDING_MISSING
    if len(rows) > 1:
        return None, PlanBlockedReason.BINDING_AMBIGUOUS
    (
        case_workspace,
        intake_id,
        intake_workspace,
        intake_revision,
        workspace_token,
        workspace_revision,
        owner_workspace,
        owner,
    ) = rows[0]
    if (
        not isinstance(case_workspace, str)
        or not case_workspace
        or not isinstance(intake_id, str)
        or not intake_id
        or not isinstance(intake_workspace, str)
        or not intake_workspace
        or not isinstance(workspace_token, str)
        or not workspace_token
        or not isinstance(owner_workspace, str)
        or not owner_workspace
        or not isinstance(owner, str)
        or not owner
        or type(intake_revision) is not int
        or type(workspace_revision) is not int
        or intake_revision < 1
        or workspace_revision < 1
        or not (case_workspace == intake_workspace == workspace_token == owner_workspace)
    ):
        return None, PlanBlockedReason.BINDING_INCONSISTENT
    return (
        _Binding(
            case_token=case_token,
            workspace_token=case_workspace,
            intake_id=intake_id,
            owner=owner,
            workspace_revision=workspace_revision,
            intake_revision=intake_revision,
        ),
        None,
    )


def _retention_decision(
    connection: sqlite3.Connection,
    case_token: str,
    evaluated_at: datetime,
    blockers: UnresolvedBlockers | None,
) -> tuple[
    RetentionDecision | None,
    CaseRetentionSnapshot | None,
    PlanBlockedReason | None,
]:
    try:
        snapshot = case_snapshot(connection, case_token)
    except RetentionTransactionRefused:
        raise _refuse(RetentionPlanRefusal.CALLER_TRANSACTION_ACTIVE) from None
    except RetentionStateInputError as error:
        reason = (
            PlanBlockedReason.UNKNOWN_CASE
            if error.reason is RetentionStateRefusal.UNKNOWN_CASE
            else PlanBlockedReason.RETENTION_STATE_UNAVAILABLE
        )
        return None, None, reason
    except RetentionStateCorrupted:
        return None, None, PlanBlockedReason.RETENTION_STATE_UNAVAILABLE
    except sqlite3.DatabaseError:
        return None, None, PlanBlockedReason.RETENTION_STATE_UNAVAILABLE

    decision = evaluate_service_retention(
        snapshot.policy.to_policy() if snapshot.policy is not None else None,
        scope=DataScope.SERVICE_DATA,
        evaluated_at=evaluated_at,
        anchors=() if snapshot.closure is None else (snapshot.closure.to_anchor(),),
        holds=tuple(hold.to_hold() for hold in snapshot.holds),
        blockers=blockers,
    )
    return decision, snapshot, None


def _content(value: object, nullable: bool) -> tuple[str | None, int | None, bool]:
    if value is None:
        return (None, None, nullable)
    if isinstance(value, bytes):
        return (hashlib.sha256(value).hexdigest(), len(value), True)
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            return (None, None, False)
        return (hashlib.sha256(encoded).hexdigest(), len(encoded), True)
    return (None, None, False)


class _Collector:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.targets: list[PlanTarget] = []
        self.refs: dict[str, int] = {}

    def add(
        self,
        category: TargetCategory,
        table: str,
        row_key: str,
        column: str,
        value: object,
        *,
        nullable: bool = False,
        valid_row: bool = True,
        base: TargetDisposition = TargetDisposition.CANDIDATE,
        unknown_schema: bool = False,
    ) -> None:
        if len(self.targets) >= self.limit:
            raise _PlanBoundReached()
        content_sha256, content_bytes, content_ok = _content(value, nullable)
        reason: str | None
        if not valid_row or not content_ok:
            disposition = TargetDisposition.PROTECTED_MALFORMED
            reason = PlanBlockedReason.ROW_MALFORMED.value
        elif unknown_schema:
            disposition = TargetDisposition.PROTECTED_UNKNOWN
            reason = PlanBlockedReason.UNKNOWN_SCHEMA_PRESENT.value
        else:
            disposition = base
            reason = (
                "immutable_requires_migration"
                if base is TargetDisposition.REQUIRES_MIGRATION
                else None
            )
        self.targets.append(
            PlanTarget(
                category=category,
                table=table,
                row_key=row_key,
                column=column,
                content_sha256=content_sha256,
                content_bytes=content_bytes,
                disposition=disposition,
                reason=reason,
            )
        )
        if isinstance(value, str):
            for ref in _PHOTO_REF.findall(value):
                self.refs[ref] = self.refs.get(ref, 0) + 1
        elif isinstance(value, bytes):
            for ref in _PHOTO_REF.findall(value.decode("latin-1", "ignore")):
                self.refs[ref] = self.refs.get(ref, 0) + 1


class _Inventory:
    """Bounded keyset-paginated inventory of this case's known ordinary service copies."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        binding: _Binding,
        states: dict[str, SourceState],
        unknown_schema: bool,
        collector: _Collector,
        batch_size: int,
    ) -> None:
        self.connection = connection
        self.binding = binding
        self.states = states
        self.unknown_schema = unknown_schema
        self.collector = collector
        self.batch_size = batch_size

    def _present(self, table: str) -> bool:
        return self.states.get(table) is SourceState.PRESENT

    def _pages(self, sql: str, params: tuple[object, ...]) -> Iterator[tuple[object, ...]]:
        after = -1
        while True:
            rows = self.connection.execute(sql, (*params, after, self.batch_size)).fetchall()
            if not rows:
                return
            last = rows[-1][0]
            if type(last) is not int:
                raise _PageMalformed()
            after = last
            for row in rows:
                yield tuple(row)

    def _add(
        self,
        category: TargetCategory,
        table: str,
        row_key: str,
        column: str,
        value: object,
        *,
        nullable: bool = False,
        valid_row: bool = True,
    ) -> None:
        base = (
            TargetDisposition.REQUIRES_MIGRATION
            if table in _REQUIRES_MIGRATION
            else TargetDisposition.CANDIDATE
        )
        self.collector.add(
            category,
            table,
            row_key,
            column,
            value,
            nullable=nullable,
            valid_row=valid_row,
            base=base,
            unknown_schema=self.unknown_schema,
        )

    def run(self) -> None:
        self._workspace()
        self._workspace_history()
        self._intake_records()
        self._drafts()
        self._research()
        self._draft_attempts()
        self._notices()
        self._ticket_operations()
        self._contact_targets()
        for table, category in (
            ("pb_address_revisions", TargetCategory.ADDRESS_REVISION),
            ("pb_address_consents", TargetCategory.ADDRESS_CONSENT),
            ("pb_address_decisions", TargetCategory.ADDRESS_DECISION),
        ):
            self._address(table, category)
        self._required_copies()
        self._approval_manifests()

    def _workspace(self) -> None:
        table = "pb_workspaces"
        if not self._present(table):
            return
        sql = (
            "SELECT rowid, workspace_token, payload_json, revision FROM pb_workspaces "
            "WHERE workspace_token = ? AND rowid > ? ORDER BY rowid LIMIT ?"
        )
        for rowid, token, payload, revision in self._pages(sql, (self.binding.workspace_token,)):
            valid = (
                isinstance(token, str)
                and token == self.binding.workspace_token
                and type(revision) is int
            )
            key = f"{self.binding.workspace_token}#{revision if valid else rowid}"
            self._add(
                TargetCategory.WORKSPACE_PAYLOAD,
                table,
                key,
                "payload_json",
                payload,
                valid_row=valid,
            )

    def _workspace_history(self) -> None:
        table = "pb_workspace_history"
        if not self._present(table):
            return
        sql = (
            "SELECT rowid, workspace_token, revision, payload_json "
            "FROM pb_workspace_history WHERE workspace_token = ? AND rowid > ? "
            "ORDER BY rowid LIMIT ?"
        )
        for rowid, token, revision, payload in self._pages(sql, (self.binding.workspace_token,)):
            valid = (
                isinstance(token, str)
                and token == self.binding.workspace_token
                and type(revision) is int
            )
            key = f"{self.binding.workspace_token}#{revision if valid else rowid}"
            self._add(
                TargetCategory.WORKSPACE_HISTORY,
                table,
                key,
                "payload_json",
                payload,
                valid_row=valid,
            )

    def _intake_records(self) -> None:
        table = "pb_intake_records"
        if not self._present(table):
            return
        sql = (
            "SELECT rowid, intake_id, intake_json, prehold_json, agency_json, revision "
            "FROM pb_intake_records WHERE intake_id = ? AND rowid > ? ORDER BY rowid LIMIT ?"
        )
        for rowid, intake_id, intake_json, prehold_json, agency_json, revision in self._pages(
            sql, (self.binding.intake_id,)
        ):
            valid = (
                isinstance(intake_id, str)
                and intake_id == self.binding.intake_id
                and type(revision) is int
            )
            scope = f"{self.binding.intake_id}#{revision if valid else rowid}"
            self._add(
                TargetCategory.INTAKE_RECORD,
                table,
                f"{scope}#intake_json",
                "intake_json",
                intake_json,
                valid_row=valid,
            )
            self._add(
                TargetCategory.INTAKE_RECORD,
                table,
                f"{scope}#prehold_json",
                "prehold_json",
                prehold_json,
                valid_row=valid,
            )
            self._add(
                TargetCategory.INTAKE_RECORD,
                table,
                f"{scope}#agency_json",
                "agency_json",
                agency_json,
                nullable=True,
                valid_row=valid,
            )

    def _drafts(self) -> None:
        table = "pb_intake_drafts"
        if not self._present(table):
            return
        sql = (
            "SELECT rowid, intake_id, letter_text, spans_json, flags_json, revision "
            "FROM pb_intake_drafts WHERE intake_id = ? AND rowid > ? ORDER BY rowid LIMIT ?"
        )
        for rowid, intake_id, letter, spans, flags, revision in self._pages(
            sql, (self.binding.intake_id,)
        ):
            valid = (
                isinstance(intake_id, str)
                and intake_id == self.binding.intake_id
                and type(revision) is int
            )
            scope = f"{self.binding.intake_id}#{revision if valid else rowid}"
            for column, value in (
                ("letter_text", letter),
                ("spans_json", spans),
                ("flags_json", flags),
            ):
                self._add(
                    TargetCategory.INTAKE_DRAFT,
                    table,
                    f"{scope}#{column}",
                    column,
                    value,
                    valid_row=valid,
                )

    def _research(self) -> None:
        table = "pb_intake_research"
        if not self._present(table):
            return
        sql = (
            "SELECT rowid, intake_id, research_json, answers_json, revision "
            "FROM pb_intake_research WHERE intake_id = ? AND rowid > ? ORDER BY rowid LIMIT ?"
        )
        for rowid, intake_id, research, answers, revision in self._pages(
            sql, (self.binding.intake_id,)
        ):
            valid = (
                isinstance(intake_id, str)
                and intake_id == self.binding.intake_id
                and type(revision) is int
            )
            scope = f"{self.binding.intake_id}#{revision if valid else rowid}"
            for column, value in (
                ("research_json", research),
                ("answers_json", answers),
            ):
                self._add(
                    TargetCategory.INTAKE_RESEARCH,
                    table,
                    f"{scope}#{column}",
                    column,
                    value,
                    valid_row=valid,
                )

    def _draft_attempts(self) -> None:
        table = "pb_draft_attempts"
        if not self._present(table):
            return
        sql = (
            "SELECT rowid, case_token, generation, attempt, result_json "
            "FROM pb_draft_attempts WHERE case_token = ? AND rowid > ? ORDER BY rowid LIMIT ?"
        )
        for rowid, token, generation, attempt, result in self._pages(
            sql, (self.binding.case_token,)
        ):
            valid = (
                isinstance(token, str)
                and token == self.binding.case_token
                and type(generation) is int
                and type(attempt) is int
            )
            key = (
                f"{self.binding.case_token}#{generation}#{attempt}"
                if valid
                else f"{self.binding.case_token}#rowid#{rowid}"
            )
            self._add(
                TargetCategory.DRAFT_ATTEMPT,
                table,
                key,
                "result_json",
                result,
                nullable=True,
                valid_row=valid,
            )

    def _notices(self) -> None:
        table = "pb_beta_feedback_notices"
        if not self._present(table):
            return
        sql = (
            "SELECT rowid, case_id, owner, notice_id, payload_json "
            "FROM pb_beta_feedback_notices WHERE case_id = ? AND rowid > ? "
            "ORDER BY rowid LIMIT ?"
        )
        for rowid, case_id, owner, notice_id, payload in self._pages(
            sql, (self.binding.intake_id,)
        ):
            valid = (
                isinstance(case_id, str)
                and case_id == self.binding.intake_id
                and owner == self.binding.owner
                and isinstance(notice_id, str)
            )
            key = notice_id if valid and isinstance(notice_id, str) else f"rowid#{rowid}"
            self._add(
                TargetCategory.BETA_FEEDBACK_NOTICE,
                table,
                key,
                "payload_json",
                payload,
                valid_row=valid,
            )

    def _ticket_operations(self) -> None:
        table = "pb_ticket_intake_operations"
        if not self._present(table):
            return
        sql = (
            "SELECT rowid, intake_id, owner, request_id, result_json "
            "FROM pb_ticket_intake_operations WHERE intake_id = ? AND rowid > ? "
            "ORDER BY rowid LIMIT ?"
        )
        for rowid, intake_id, owner, request_id, result in self._pages(
            sql, (self.binding.intake_id,)
        ):
            valid = (
                isinstance(intake_id, str)
                and intake_id == self.binding.intake_id
                and owner == self.binding.owner
                and isinstance(request_id, str)
            )
            key = request_id if isinstance(request_id, str) else f"rowid#{rowid}"
            self._add(
                TargetCategory.TICKET_INTAKE_OPERATION,
                table,
                key,
                "result_json",
                result,
                nullable=True,
                valid_row=valid,
            )

    def _contact_targets(self) -> None:
        table = "pb_beta_contact_targets"
        if not self._present(table):
            return
        sql = (
            "SELECT rowid, case_id, owner, target_id, revision, contact_value "
            "FROM pb_beta_contact_targets WHERE case_id = ? AND rowid > ? "
            "ORDER BY rowid LIMIT ?"
        )
        for rowid, case_id, owner, target_id, revision, value in self._pages(
            sql, (self.binding.intake_id,)
        ):
            valid = (
                isinstance(case_id, str)
                and case_id == self.binding.intake_id
                and owner == self.binding.owner
                and isinstance(target_id, str)
                and type(revision) is int
            )
            key = (
                f"{target_id}#{revision}"
                if valid and isinstance(target_id, str)
                else f"rowid#{rowid}"
            )
            self._add(
                TargetCategory.BETA_CONTACT_TARGET,
                table,
                key,
                "contact_value",
                value,
                valid_row=valid,
            )

    def _address(self, table: str, category: TargetCategory) -> None:
        if not self._present(table):
            return
        sql = (
            f"SELECT rowid, case_id, owner, revision, payload_json FROM {_quote(table)} "
            "WHERE case_id = ? AND rowid > ? ORDER BY rowid LIMIT ?"
        )
        for rowid, case_id, owner, revision, payload in self._pages(sql, (self.binding.intake_id,)):
            valid = (
                isinstance(case_id, str)
                and case_id == self.binding.intake_id
                and owner == self.binding.owner
                and type(revision) is int
            )
            key = f"{self.binding.intake_id}#{revision if valid else rowid}"
            self._add(category, table, key, "payload_json", payload, valid_row=valid)

    def _required_copies(self) -> None:
        table = "pb_required_copies"
        if not self._present(table):
            return
        sql = (
            "SELECT rowid, case_id, owner, revision, selections_json "
            "FROM pb_required_copies WHERE case_id = ? AND rowid > ? ORDER BY rowid LIMIT ?"
        )
        for rowid, case_id, owner, revision, selections in self._pages(
            sql, (self.binding.intake_id,)
        ):
            valid = (
                isinstance(case_id, str)
                and case_id == self.binding.intake_id
                and owner == self.binding.owner
                and type(revision) is int
            )
            key = f"{self.binding.intake_id}#{revision if valid else rowid}"
            self._add(
                TargetCategory.REQUIRED_COPIES,
                table,
                key,
                "selections_json",
                selections,
                valid_row=valid,
            )

    def _approval_manifests(self) -> None:
        table = "pb_approval_manifests"
        if not self._present(table):
            return
        rows = self.connection.execute(
            "SELECT * FROM pb_approval_manifests WHERE case_token = ? ORDER BY attempt",
            (self.binding.case_token,),
        )
        for row in rows:
            attempt = row[1]
            self._add(
                TargetCategory.APPROVAL_MANIFEST,
                table,
                f"{self.binding.case_token}#{attempt}",
                "manifest_json",
                _canonical(list(row)),
                valid_row=type(attempt) is int and attempt in {1, 2},
            )


def _surviving_reference(connection: sqlite3.Connection, binding: _Binding, ref: str) -> bool:
    """Whether any known source row for the same owner, outside this case, names the ref."""

    queries: tuple[tuple[str, tuple[object, ...]], ...] = (
        (
            "SELECT 1 FROM pb_workspaces r JOIN pb_intake_owners o "
            "ON o.workspace_token = r.workspace_token WHERE o.owner = ? "
            "AND r.workspace_token <> ? AND instr(r.payload_json, ?) > 0 LIMIT 1",
            (binding.owner, binding.workspace_token, ref),
        ),
        (
            "SELECT 1 FROM pb_workspace_history r JOIN pb_intake_owners o "
            "ON o.workspace_token = r.workspace_token WHERE o.owner = ? "
            "AND r.workspace_token <> ? AND instr(r.payload_json, ?) > 0 LIMIT 1",
            (binding.owner, binding.workspace_token, ref),
        ),
        (
            "SELECT 1 FROM pb_intake_records r JOIN pb_intake_owners o "
            "ON o.workspace_token = r.workspace_token WHERE o.owner = ? "
            "AND r.intake_id <> ? AND (instr(r.intake_json, ?) + "
            "instr(r.prehold_json, ?) + instr(r.agency_json, ?)) > 0 LIMIT 1",
            (binding.owner, binding.intake_id, ref, ref, ref),
        ),
        (
            "SELECT 1 FROM pb_intake_drafts r JOIN pb_intake_records i "
            "ON i.intake_id = r.intake_id JOIN pb_intake_owners o "
            "ON o.workspace_token = i.workspace_token WHERE o.owner = ? "
            "AND r.intake_id <> ? AND (instr(r.letter_text, ?) + "
            "instr(r.spans_json, ?) + instr(r.flags_json, ?)) > 0 LIMIT 1",
            (binding.owner, binding.intake_id, ref, ref, ref),
        ),
        (
            "SELECT 1 FROM pb_intake_research r JOIN pb_intake_records i "
            "ON i.intake_id = r.intake_id JOIN pb_intake_owners o "
            "ON o.workspace_token = i.workspace_token WHERE o.owner = ? "
            "AND r.intake_id <> ? AND (instr(r.research_json, ?) + "
            "instr(r.answers_json, ?)) > 0 LIMIT 1",
            (binding.owner, binding.intake_id, ref, ref),
        ),
        (
            "SELECT 1 FROM pb_required_copies WHERE owner = ? AND case_id <> ? "
            "AND instr(selections_json, ?) > 0 LIMIT 1",
            (binding.owner, binding.intake_id, ref),
        ),
        (
            "SELECT 1 FROM pb_beta_feedback_notices WHERE owner = ? AND case_id <> ? "
            "AND instr(payload_json, ?) > 0 LIMIT 1",
            (binding.owner, binding.intake_id, ref),
        ),
        (
            "SELECT 1 FROM pb_draft_attempts WHERE case_token <> ? "
            "AND instr(result_json, ?) > 0 LIMIT 1",
            (binding.case_token, ref),
        ),
        (
            "SELECT 1 FROM pb_ticket_intake_operations WHERE owner = ? AND intake_id <> ? "
            "AND instr(result_json, ?) > 0 LIMIT 1",
            (binding.owner, binding.intake_id, ref),
        ),
        (
            "SELECT 1 FROM pb_beta_contact_targets WHERE owner = ? AND case_id <> ? "
            "AND instr(contact_value, ?) > 0 LIMIT 1",
            (binding.owner, binding.intake_id, ref),
        ),
        (
            "SELECT 1 FROM pb_address_revisions WHERE owner = ? AND case_id <> ? "
            "AND instr(payload_json, ?) > 0 LIMIT 1",
            (binding.owner, binding.intake_id, ref),
        ),
        (
            "SELECT 1 FROM pb_address_consents WHERE owner = ? AND case_id <> ? "
            "AND instr(payload_json, ?) > 0 LIMIT 1",
            (binding.owner, binding.intake_id, ref),
        ),
        (
            "SELECT 1 FROM pb_address_decisions WHERE owner = ? AND case_id <> ? "
            "AND instr(payload_json, ?) > 0 LIMIT 1",
            (binding.owner, binding.intake_id, ref),
        ),
    )
    for sql, params in queries:
        try:
            if connection.execute(sql, params).fetchone() is not None:
                return True
        except sqlite3.DatabaseError:
            return True
    return False


def _blob_targets(
    connection: sqlite3.Connection,
    binding: _Binding,
    refs: dict[str, int],
    unknown_schema: bool,
    sources_unavailable: bool,
    blob_store: BlobStoreLike | None,
) -> tuple[BlobTarget, ...]:
    owner_dir = hashlib.sha256(binding.owner.encode("utf-8")).hexdigest()
    targets: list[BlobTarget] = []
    for ref in sorted(refs):
        if unknown_schema:
            disposition = BlobDisposition.PROTECTED_UNKNOWN_SCOPE
            reason = BlobReason.UNKNOWN_SCHEMA.value
        elif sources_unavailable:
            disposition = BlobDisposition.PROTECTED_UNKNOWN_SCOPE
            reason = BlobReason.SOURCE_UNAVAILABLE.value
        elif _surviving_reference(connection, binding, ref):
            disposition = BlobDisposition.RETAINED_SHARED
            reason = BlobReason.SHARED_REFERENCE.value
        elif blob_store is None:
            disposition = BlobDisposition.PROTECTED_PRESENCE_UNKNOWN
            reason = BlobReason.PRESENCE_UNKNOWN.value
        else:
            try:
                data = blob_store.get(binding.owner, ref)
            except Exception:  # a store fault protects the blob; it is never a candidate
                disposition = BlobDisposition.PROTECTED_UNREADABLE
                reason = BlobReason.UNREADABLE.value
            else:
                if data is None:
                    disposition = BlobDisposition.MISSING_OR_ORPHAN
                    reason = BlobReason.MISSING_BYTES.value
                elif not isinstance(data, bytes):
                    disposition = BlobDisposition.PROTECTED_UNREADABLE
                    reason = BlobReason.UNREADABLE.value
                elif "photo_" + hashlib.sha256(data).hexdigest() != ref:
                    disposition = BlobDisposition.PROTECTED_DIGEST_MISMATCH
                    reason = BlobReason.DIGEST_MISMATCH.value
                else:
                    disposition = BlobDisposition.CANDIDATE
                    reason = None
        targets.append(
            BlobTarget(
                owner_dir=owner_dir,
                ref=ref,
                disposition=disposition,
                reason=reason,
                reference_count=refs[ref],
            )
        )
    return tuple(targets)


def _retained_scopes(
    connection: sqlite3.Connection, kinds: dict[str, str], binding: _Binding | None
) -> tuple[RetainedScope, ...]:
    scopes: list[RetainedScope] = []
    for scope, table, column in _RESTRICTED_SCOPES:
        if table not in kinds or kinds[table] != "table":
            scopes.append(RetainedScope(scope=scope, table=table, present=False, row_count=None))
            continue
        columns = {name for name, _type in _table_info(connection, table)}
        if column is None:
            try:
                row = connection.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()
            except sqlite3.DatabaseError:
                row = None
            count = row[0] if row is not None and type(row[0]) is int else None
            scopes.append(RetainedScope(scope=scope, table=table, present=True, row_count=count))
            continue
        if binding is None or column not in columns:
            scopes.append(RetainedScope(scope=scope, table=table, present=True, row_count=None))
            continue
        value = binding.case_token if column == "case_token" else binding.intake_id
        try:
            row = connection.execute(
                f"SELECT COUNT(*) FROM {_quote(table)} WHERE {_quote(column)} = ?", (value,)
            ).fetchone()
        except sqlite3.DatabaseError:
            count = None
        else:
            count = row[0] if row is not None and type(row[0]) is int else None
        scopes.append(RetainedScope(scope=scope, table=table, present=True, row_count=count))
    return tuple(scopes)


def _policy_digest(snapshot: CaseRetentionSnapshot | None) -> str | None:
    if snapshot is None or snapshot.policy is None:
        return None
    policy = snapshot.policy
    return _digest(
        {
            "version": policy.version,
            "adoption": policy.adoption.value,
            "timezone": policy.timezone.key,
            "retention_years": policy.retention_years,
            "adoption_evidence_ref": policy.adoption_evidence_ref,
            "recorded_at": _timestamp_text(policy.recorded_at),
        }
    )


def _closure_digest(snapshot: CaseRetentionSnapshot | None) -> str | None:
    if snapshot is None or snapshot.closure is None:
        return None
    closure = snapshot.closure
    return _digest(
        {
            "case_token": closure.case_token,
            "generation": closure.generation,
            "closure_kind": closure.kind.value,
            "anchor_at": _timestamp_text(closure.anchor_at),
            "source_ref": closure.source_ref,
            "recorded_at": _timestamp_text(closure.recorded_at),
        }
    )


def _holds_digest(snapshot: CaseRetentionSnapshot | None) -> str | None:
    if snapshot is None:
        return None
    return _digest(
        [
            {
                "case_token": hold.case_token,
                "hold_id": hold.hold_id,
                "generation": hold.generation,
                "scope": hold.scope.value,
                "active": hold.active,
                "source_ref": hold.source_ref,
                "held_at": _timestamp_text(hold.held_at),
                "released_at": _timestamp_text(hold.released_at),
            }
            for hold in sorted(snapshot.holds, key=lambda item: item.hold_id)
        ]
    )


def _binding_digest(binding: _Binding | None) -> str | None:
    if binding is None:
        return None
    return _digest(
        {
            "case_token": binding.case_token,
            "workspace_token": binding.workspace_token,
            "intake_id": binding.intake_id,
            "owner_dir": hashlib.sha256(binding.owner.encode("utf-8")).hexdigest(),
            "workspace_revision": binding.workspace_revision,
            "intake_revision": binding.intake_revision,
        }
    )


def _unique(reasons: list[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.append(reason)
    return tuple(seen)


def _summarise(
    *,
    blocked: tuple[str, ...],
    partial: bool,
    states: dict[str, SourceState],
    unknown: tuple[str, ...],
    targets: tuple[PlanTarget, ...],
    blobs: tuple[BlobTarget, ...],
    retained: tuple[RetainedScope, ...],
) -> RetentionPlanSummary:
    target_counts = tuple(
        (
            category.value,
            sum(1 for target in targets if target.category is category),
        )
        for category in TargetCategory
    )
    disposition_counts = tuple(
        (
            disposition.value,
            sum(1 for target in targets if target.disposition is disposition),
        )
        for disposition in TargetDisposition
    )
    blob_counts = tuple(
        (
            disposition.value,
            sum(1 for blob in blobs if blob.disposition is disposition),
        )
        for disposition in BlobDisposition
    )
    return RetentionPlanSummary(
        format_version=FORMAT_VERSION,
        eligible=not blocked and not partial,
        blocked_reasons=blocked,
        partial=partial,
        source_present_count=sum(1 for state in states.values() if state is SourceState.PRESENT),
        source_missing_count=sum(1 for state in states.values() if state is SourceState.MISSING),
        source_malformed_count=sum(
            1 for state in states.values() if state is SourceState.MALFORMED
        ),
        unknown_table_count=len(unknown),
        target_count=len(targets),
        target_counts=target_counts,
        disposition_counts=disposition_counts,
        blob_count=len(blobs),
        blob_counts=blob_counts,
        retained_scopes=tuple((scope.scope, scope.present, scope.row_count) for scope in retained),
        outstanding_scopes=OUTSTANDING_SCOPES,
    )


def _build_plan(
    connection: sqlite3.Connection,
    case_token: str,
    evaluated_at: datetime,
    blockers: UnresolvedBlockers | None,
    blob_store: BlobStoreLike | None,
    batch_size: int,
    max_plan_rows: int,
) -> RetentionPlan:
    states, kinds, unknown, components, schema_objects, schema_objects_malformed = _schema_snapshot(
        connection
    )
    source_fingerprint = _source_fingerprint(schema_objects, unknown, components)
    unknown_schema = bool(unknown)

    blocked: list[str] = []
    sources_unavailable = any(state is not SourceState.PRESENT for state in states.values())
    if any(state is SourceState.MISSING for state in states.values()):
        blocked.append(PlanBlockedReason.SOURCE_TABLE_MISSING.value)
    if schema_objects_malformed or any(state is SourceState.MALFORMED for state in states.values()):
        blocked.append(PlanBlockedReason.SOURCE_SCHEMA_MALFORMED.value)
    if unknown_schema:
        blocked.append(PlanBlockedReason.UNKNOWN_SCHEMA_PRESENT.value)

    binding, binding_reason = _resolve_binding(connection, case_token, states)
    if binding_reason is not None:
        blocked.append(binding_reason.value)

    targets: tuple[PlanTarget, ...] = ()
    blobs: tuple[BlobTarget, ...] = ()
    partial = False
    if binding is not None:
        collector = _Collector(max_plan_rows)
        inventory = _Inventory(connection, binding, states, unknown_schema, collector, batch_size)
        try:
            inventory.run()
        except _PlanBoundReached:
            partial = True
            blocked.append(PlanBlockedReason.PLAN_BOUND_EXCEEDED.value)
        except _PageMalformed:
            partial = True
            blocked.append(PlanBlockedReason.ROW_MALFORMED.value)
        if any(
            target.disposition is TargetDisposition.PROTECTED_MALFORMED
            for target in collector.targets
        ):
            blocked.append(PlanBlockedReason.ROW_MALFORMED.value)
        targets = tuple(collector.targets)
        blobs = _blob_targets(
            connection,
            binding,
            collector.refs,
            unknown_schema,
            sources_unavailable,
            blob_store,
        )

    decision, snapshot, state_reason = _retention_decision(
        connection, case_token, evaluated_at, blockers
    )
    if state_reason is not None:
        blocked.append(state_reason.value)
    if decision is not None:
        for refusal in decision.refusals:
            blocked.append(refusal.value)

    blocked_reasons = _unique(blocked)
    retained = _retained_scopes(connection, kinds, binding)
    summary = _summarise(
        blocked=blocked_reasons,
        partial=partial,
        states=states,
        unknown=unknown,
        targets=targets,
        blobs=blobs,
        retained=retained,
    )

    plan_digest = _digest(
        {
            "format_version": FORMAT_VERSION,
            "evaluated_at": _timestamp_text(evaluated_at),
            "source_fingerprint": source_fingerprint,
            "binding": _binding_digest(binding),
            "policy": _policy_digest(snapshot),
            "closure": _closure_digest(snapshot),
            "holds": _holds_digest(snapshot),
            "blockers": (
                None
                if blockers is None
                else (
                    {
                        "money": blockers.money,
                        "submission": blockers.submission,
                        "original_mail": blockers.original_mail,
                        "deadline": blockers.deadline,
                        "incident": blockers.incident,
                        "recovery": blockers.recovery,
                    }
                    if isinstance(blockers, UnresolvedBlockers)
                    else "invalid"
                )
            ),
            "decision": None if decision is None else decision.to_dict(),
            "blocked_reasons": list(blocked_reasons),
            "partial": partial,
            "max_plan_rows": max_plan_rows,
            "targets": [
                [
                    target.category.value,
                    target.table,
                    target.row_key,
                    target.column,
                    target.content_sha256,
                    target.content_bytes,
                    target.disposition.value,
                    target.reason,
                ]
                for target in sorted(
                    targets,
                    key=lambda item: (item.category.value, item.table, item.row_key, item.column),
                )
            ],
            "blobs": [
                [
                    blob.owner_dir,
                    blob.ref,
                    blob.disposition.value,
                    blob.reason,
                    blob.reference_count,
                ]
                for blob in blobs
            ],
            "retained_scopes": [
                [scope.scope, scope.table, scope.present, scope.row_count] for scope in retained
            ],
        }
    )

    return RetentionPlan(
        format_version=FORMAT_VERSION,
        case_token=case_token,
        workspace_token=None if binding is None else binding.workspace_token,
        intake_id=None if binding is None else binding.intake_id,
        owner_dir=(
            None if binding is None else hashlib.sha256(binding.owner.encode("utf-8")).hexdigest()
        ),
        plan_digest=plan_digest,
        source_fingerprint=source_fingerprint,
        binding_digest=_binding_digest(binding),
        policy_revision_digest=_policy_digest(snapshot),
        closure_revision_digest=_closure_digest(snapshot),
        hold_revisions_digest=_holds_digest(snapshot),
        decision_digest=_digest(None if decision is None else decision.to_dict()),
        partial=partial,
        source_statuses=tuple(
            SourceStatus(table=table, state=state) for table, state in sorted(states.items())
        ),
        targets=targets,
        blobs=blobs,
        retained_scopes=retained,
        summary=summary,
    )


def plan_service_data_retention(
    database: sqlite3.Connection,
    *,
    case_token: str,
    evaluated_at: datetime,
    blockers: UnresolvedBlockers | None,
    blob_store: BlobStoreLike | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_plan_rows: int = DEFAULT_MAX_PLAN_ROWS,
) -> RetentionPlan:
    """Build a read-only dry-run plan for one case without touching any stored data.

    The connection must not already own a transaction: this function refuses instead of
    committing or rolling back caller work, then opens and closes its own read transaction.
    ``blockers`` must be supplied explicitly; ``None`` is missing input, never a clean fact.
    The result is a private manifest bound by ``plan_digest`` and a content-free summary.
    Nothing is deleted, migrated, scheduled or authorized.
    """

    if not isinstance(database, sqlite3.Connection):
        raise _refuse(RetentionPlanRefusal.SOURCE_UNAVAILABLE)
    case = _check_case_token(case_token)
    batch = _check_batch_size(batch_size)
    bound = _check_plan_bound(max_plan_rows)
    if database.in_transaction:
        raise _refuse(RetentionPlanRefusal.CALLER_TRANSACTION_ACTIVE)
    try:
        database.execute("BEGIN")
    except sqlite3.DatabaseError:
        raise _refuse(RetentionPlanRefusal.SOURCE_UNAVAILABLE) from None
    try:
        plan = _build_plan(database, case, evaluated_at, blockers, blob_store, batch, bound)
        database.execute("COMMIT")
    except BaseException as error:
        if database.in_transaction:
            with contextlib.suppress(sqlite3.Error):
                database.execute("ROLLBACK")
        if isinstance(error, sqlite3.DatabaseError):
            raise _refuse(RetentionPlanRefusal.SOURCE_UNAVAILABLE) from None
        raise
    return plan


__all__ = [
    "COMPONENT",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_PLAN_ROWS",
    "FORMAT_VERSION",
    "MAX_BATCH_SIZE",
    "OUTSTANDING_SCOPES",
    "BlobDisposition",
    "BlobReason",
    "BlobStoreLike",
    "BlobTarget",
    "PlanBlockedReason",
    "PlanTarget",
    "RetainedScope",
    "RetentionPlan",
    "RetentionPlanRefusal",
    "RetentionPlanRefused",
    "RetentionPlanSummary",
    "SourceState",
    "SourceStatus",
    "TargetCategory",
    "TargetDisposition",
    "plan_service_data_retention",
]
