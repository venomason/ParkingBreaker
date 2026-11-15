"""Explicit, synthetic-only execution of a version-1 retention plan.

No scheduler, filesystem store, provider or application route is installed here. Callers
must supply a synthetic database and an owner-aware, idempotent BlobDeleteStore. The
existing planner calls its manifest ``RetentionPlan``; RetentionPlanManifest is an alias.
``planned_at`` and ``max_plan_rows`` must be the original planner inputs, since version 1
binds them in its digest without retaining them in its dataclass.

Migration strategy ``payload_tombstone_v1`` preserves rows and all non-payload audit
columns. Under BEGIN IMMEDIATE, only recognized immutability triggers are suspended,
payloads become NULL or a fixed redaction marker, and the exact triggers are restored
before commit. Schema checks, a durable fence and append-only per-target digest receipts
make the exception auditable. No persistent trigger bypass or writable_schema is used.
The actual notice table is pb_beta_feedback_notices. Invented/legacy notice table names
are unknown schemas and refuse execution rather than silently omitting their contents.

Private fence state retains opaque case/workspace/intake routing keys, never customer
payloads or blob paths. Public receipts contain only counts, digests and fixed categories.
Normal access must enter through guarded_service_read; restore must validate against an
independent, current authority database, never solely the old backup. These are explicit
integration primitives, not changes to existing API/store/backup entry points. Logical
redaction does not erase WAL, free pages, backups, caches, PDFs or provider-held copies.
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Protocol

from parkingbreaker import retention_plan as rp
from parkingbreaker import retention_state as rs
from parkingbreaker.approval_manifest import (
    _SCHEMA as APPROVAL_MANIFEST_SCHEMA,
)
from parkingbreaker.approval_manifest import (
    APPROVAL_MANIFEST_TRIGGER_NAMES,
    APPROVAL_MANIFEST_TRIGGER_STATEMENTS,
)
from parkingbreaker.lob_submission import Uncertain
from parkingbreaker.payment_records import decode_cursor
from parkingbreaker.retention_policy import (
    DataScope,
    RetentionDecision,
    UnresolvedBlockers,
    evaluate_service_retention,
)
from parkingbreaker.submission_records import decode_outcome

RetentionPlanManifest = rp.RetentionPlan
SCHEMA_VERSION = 1
REDACTED_JSON = '{"retention":"purged_v1"}'


class BlobDeleteStore(Protocol):
    """Synthetic, owner-scoped store; delete must succeed when already absent.

    Implementations must not write to SQLite or invoke providers. The executor holds the
    database write lock during get/delete/verification to serialize reference/hold changes.
    Absence is verified with get after deletion, including ambiguous failures on retry.
    """

    def get(self, owner: str, ref: str) -> bytes | None: ...

    def delete(self, owner: str, ref: str) -> None: ...


class PurgeRefusal(enum.StrEnum):
    SYNTHETIC_ONLY = "synthetic_only"
    CALLER_TRANSACTION_ACTIVE = "caller_transaction_active"
    INVALID_INPUT = "invalid_input"
    PLAN_DIGEST_MISMATCH = "plan_digest_mismatch"
    PLAN_NOT_ELIGIBLE = "plan_not_eligible"
    REVISION_DRIFT = "revision_drift"
    CONTENT_DRIFT = "content_drift"
    SCHEMA_DRIFT = "schema_drift"
    HOLD_ACTIVE = "hold_active"
    UNRESOLVED_EFFECTS = "unresolved_effects"
    SOURCE_UNAVAILABLE = "source_unavailable"
    ACCESS_FENCED = "access_fenced"
    RESTORE_EXCLUDED = "restore_excluded"


class RetentionPurgeRefused(ValueError):
    def __init__(self, reason: PurgeRefusal) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclasses.dataclass(frozen=True)
class PurgeReceipt:
    plan_digest: str
    status: str
    target_count: int
    redacted_count: int
    blob_count: int
    blob_counts: tuple[tuple[str, int], ...]
    category_counts: tuple[tuple[str, int], ...]
    migration: str = "payload_tombstone_v1"
    outstanding_scopes: tuple[str, ...] = rp.OUTSTANDING_SCOPES
    service_purge_complete: bool = False

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


_DDL = {
    "pb_purge_cases": """CREATE TABLE pb_purge_cases (
        case_digest TEXT PRIMARY KEY, case_token TEXT NOT NULL UNIQUE,
        workspace_token TEXT NOT NULL UNIQUE, intake_id TEXT NOT NULL UNIQUE,
        plan_digest TEXT NOT NULL, manifest_digest TEXT NOT NULL,
        schema_version INTEGER NOT NULL CHECK (schema_version = 1)
    )""",
    "pb_purge_targets": """CREATE TABLE pb_purge_targets (
        case_digest TEXT NOT NULL, sequence INTEGER NOT NULL,
        category TEXT NOT NULL, before_digest TEXT, after_digest TEXT,
        PRIMARY KEY (case_digest, sequence)
    )""",
    "pb_purge_blobs": """CREATE TABLE pb_purge_blobs (
        case_digest TEXT NOT NULL, blob_digest TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK
            (outcome IN ('deleted', 'absent', 'shared', 'restricted', 'protected', 'failed')),
        PRIMARY KEY (case_digest, blob_digest)
    )""",
}

# All names/SQL in this registry come from the existing store implementations. A new
# trigger requires explicit review, even if a caller has obtained a new plan digest.
_MIGRATION_TRIGGERS = {
    "pb_draft_attempts_no_delete": (
        "CREATE TRIGGER pb_draft_attempts_no_delete BEFORE DELETE ON pb_draft_attempts "
        "BEGIN SELECT RAISE(ABORT, 'draft attempts are append-only'); END"
    ),
    "pb_draft_attempts_finish_once": (
        "CREATE TRIGGER pb_draft_attempts_finish_once BEFORE UPDATE ON pb_draft_attempts "
        "WHEN OLD.outcome IS NOT NULL OR NEW.started_at IS NOT OLD.started_at "
        "BEGIN SELECT RAISE(ABORT, 'draft attempts finish once'); END"
    ),
    "pb_beta_feedback_notices_no_update": (
        "CREATE TRIGGER pb_beta_feedback_notices_no_update BEFORE UPDATE "
        "ON pb_beta_feedback_notices "
        "BEGIN SELECT RAISE(ABORT, 'beta feedback notice is immutable'); END"
    ),
    "pb_beta_feedback_notices_no_delete": (
        "CREATE TRIGGER pb_beta_feedback_notices_no_delete BEFORE DELETE "
        "ON pb_beta_feedback_notices "
        "BEGIN SELECT RAISE(ABORT, 'beta feedback notice is immutable'); END"
    ),
    "pb_beta_contact_targets_no_delete": (
        "CREATE TRIGGER pb_beta_contact_targets_no_delete BEFORE DELETE "
        "ON pb_beta_contact_targets "
        "BEGIN SELECT RAISE(ABORT, 'beta contact targets are kept'); END"
    ),
    "pb_ticket_intake_operations_no_delete": (
        "CREATE TRIGGER pb_ticket_intake_operations_no_delete BEFORE DELETE "
        "ON pb_ticket_intake_operations "
        "BEGIN SELECT RAISE(ABORT, 'ticket intake claims are append-only'); END"
    ),
    "pb_ticket_intake_operations_complete_once": (
        "CREATE TRIGGER pb_ticket_intake_operations_complete_once BEFORE UPDATE "
        "ON pb_ticket_intake_operations WHEN OLD.status != 'pending' "
        "OR NEW.request_id IS NOT OLD.request_id OR NEW.intake_id IS NOT OLD.intake_id "
        "OR NEW.workspace_token IS NOT OLD.workspace_token OR NEW.owner IS NOT OLD.owner "
        "OR NEW.operation IS NOT OLD.operation OR NEW.binding_sha256 IS NOT OLD.binding_sha256 "
        "OR NEW.claim_token_sha256 IS NOT OLD.claim_token_sha256 "
        "OR NEW.claimed_at IS NOT OLD.claimed_at OR NEW.status = 'pending' "
        "BEGIN SELECT RAISE(ABORT, 'ticket intake claims complete once'); END"
    ),
}

# A routing column, its corresponding fence column, and the allowed payload columns.
_SOURCES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "pb_workspaces": ("workspace_token", "workspace_token", ("payload_json",)),
    "pb_workspace_history": ("workspace_token", "workspace_token", ("payload_json",)),
    "pb_intake_records": ("intake_id", "intake_id", ("intake_json", "prehold_json", "agency_json")),
    "pb_intake_drafts": ("intake_id", "intake_id", ("letter_text", "spans_json", "flags_json")),
    "pb_intake_research": ("intake_id", "intake_id", ("research_json", "answers_json")),
    "pb_draft_attempts": ("case_token", "case_token", ("result_json",)),
    "pb_beta_feedback_notices": ("case_id", "intake_id", ("payload_json",)),
    "pb_ticket_intake_operations": ("intake_id", "intake_id", ("result_json",)),
    "pb_beta_contact_targets": ("case_id", "intake_id", ("contact_value",)),
    "pb_address_revisions": ("case_id", "intake_id", ("payload_json",)),
    "pb_address_consents": ("case_id", "intake_id", ("payload_json",)),
    "pb_address_decisions": ("case_id", "intake_id", ("payload_json",)),
    "pb_required_copies": ("case_id", "intake_id", ("selections_json",)),
}


def _fail(reason: PurgeRefusal) -> RetentionPurgeRefused:
    return RetentionPurgeRefused(reason)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized(sql: str) -> str:
    return " ".join(sql.replace("IF NOT EXISTS ", "").upper().split())


def _fence_ddl() -> dict[str, str]:
    result: dict[str, str] = {}
    routes: dict[str, tuple[tuple[str, str], ...]] = {
        table: ((column, fence_column),)
        for table, (column, fence_column, _payloads) in _SOURCES.items()
    }
    routes["pb_cases"] = (("case_token", "case_token"), ("workspace_token", "workspace_token"))
    routes["pb_intake_owners"] = (("workspace_token", "workspace_token"),)
    for table, keys in routes.items():
        for action in ("INSERT", "UPDATE", "DELETE"):
            sides = (
                ("NEW", "OLD")
                if action == "UPDATE"
                else (("OLD",) if action == "DELETE" else ("NEW",))
            )
            predicate = " OR ".join(
                f"EXISTS (SELECT 1 FROM pb_purge_cases WHERE {fence_column} = {side}.{column})"
                for side in sides
                for column, fence_column in keys
            )
            name = f"pb_purge_fence_{table}_{action.lower()}"
            result[name] = (
                f"CREATE TRIGGER {name} BEFORE {action} ON {table} WHEN {predicate} "
                "BEGIN SELECT RAISE(ABORT, 'retention_access_fenced'); END"
            )
    for table in ("pb_purge_cases", "pb_purge_targets"):
        for action in ("UPDATE", "DELETE"):
            name = f"{table}_no_{action.lower()}"
            result[name] = (
                f"CREATE TRIGGER {name} BEFORE {action} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'retention_receipt_immutable'); END"
            )
        # SQLite REPLACE can delete conflicting rows without firing DELETE triggers when
        # recursive_triggers is off. Refuse the conflicting INSERT itself as well.
        receipt_keys = (
            ("case_digest", "case_token", "workspace_token", "intake_id")
            if table == ("pb_purge_cases")
            else ("case_digest", "sequence")
        )
        join = " OR " if table == "pb_purge_cases" else " AND "
        predicate = join.join(f"{key} = NEW.{key}" for key in receipt_keys)
        name = f"{table}_no_replace"
        result[name] = (
            f"CREATE TRIGGER {name} BEFORE INSERT ON {table} "
            f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {predicate}) "
            "BEGIN SELECT RAISE(ABORT, 'retention_receipt_immutable'); END"
        )
    return result


_FENCES = _fence_ddl()
_APPROVAL_MANIFEST_FENCE_NAME = "pb_approval_manifests_purged_case_fence"
_APPROVAL_MANIFEST_FENCE_DDL = (
    f"CREATE TRIGGER IF NOT EXISTS {_APPROVAL_MANIFEST_FENCE_NAME} "
    "BEFORE INSERT ON pb_approval_manifests "
    "WHEN EXISTS (SELECT 1 FROM pb_purge_cases WHERE case_token = NEW.case_token) "
    "BEGIN SELECT RAISE(ABORT, 'purged case is fenced'); END"
)


def _has_approval_manifests(database: sqlite3.Connection) -> bool:
    return (
        database.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pb_approval_manifests'"
        ).fetchone()
        is not None
    )


def _ensure_approval_manifest_fence(database: sqlite3.Connection) -> None:
    if _has_approval_manifests(database):
        database.execute(_APPROVAL_MANIFEST_FENCE_DDL)


def _schema_ready(database: sqlite3.Connection) -> bool:
    objects = database.execute(
        "SELECT name, sql FROM sqlite_master WHERE name GLOB 'pb_purge_*'"
    ).fetchall()
    if not objects:
        return False
    expected = {**_DDL, **_FENCES}
    if len(objects) != len(expected) or any(
        name not in expected
        or not isinstance(sql, str)
        or _normalized(sql) != _normalized(expected[name])
        for name, sql in objects
    ):
        raise _fail(PurgeRefusal.SCHEMA_DRIFT)
    return True


@contextlib.contextmanager
def _transaction(database: sqlite3.Connection, *, write: bool) -> Iterator[None]:
    if database.in_transaction:
        raise _fail(PurgeRefusal.CALLER_TRANSACTION_ACTIVE)
    try:
        database.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield
        database.execute("COMMIT")
    except BaseException as error:
        if database.in_transaction:
            with contextlib.suppress(sqlite3.Error):
                database.execute("ROLLBACK")
        if isinstance(
            error, (sqlite3.Error, rs.RetentionStateCorrupted, rs.RetentionStateInputError)
        ):
            raise _fail(PurgeRefusal.SOURCE_UNAVAILABLE) from None
        raise


def _source_schema(database: sqlite3.Connection) -> tuple[dict[str, rp.SourceState], str]:
    ready = _schema_ready(database)
    states, _kinds, unknown, components, objects, malformed = rp._schema_snapshot(database)
    if _has_approval_manifests(database):
        ddl = database.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'pb_approval_manifests'"
        ).fetchone()
        expected_triggers = {
            name: _normalized(statement).rstrip(";")
            for name, statement in zip(
                APPROVAL_MANIFEST_TRIGGER_NAMES,
                APPROVAL_MANIFEST_TRIGGER_STATEMENTS,
                strict=True,
            )
        }
        actual_triggers = dict(
            database.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'pb_approval_manifests'"
            )
        )
        if _APPROVAL_MANIFEST_FENCE_NAME in actual_triggers:
            fence_sql = actual_triggers.pop(_APPROVAL_MANIFEST_FENCE_NAME)
            if _normalized(fence_sql) != _normalized(_APPROVAL_MANIFEST_FENCE_DDL):
                raise _fail(PurgeRefusal.SCHEMA_DRIFT)
        if (
            ddl is None
            or not isinstance(ddl[0], str)
            or _normalized(ddl[0]).rstrip(";") != _normalized(APPROVAL_MANIFEST_SCHEMA).rstrip(";")
            or {name: _normalized(sql).rstrip(";") for name, sql in actual_triggers.items()}
            != expected_triggers
        ):
            raise _fail(PurgeRefusal.SCHEMA_DRIFT)
    own = set(_DDL) | set(_FENCES) | {_APPROVAL_MANIFEST_FENCE_NAME} if ready else set()
    unknown = tuple(name for name in unknown if name not in own)
    objects = tuple(item for item in objects if item[1] not in own)
    if (
        unknown
        or malformed
        or any(state is not rp.SourceState.PRESENT for state in states.values())
    ):
        raise _fail(PurgeRefusal.SCHEMA_DRIFT)
    for kind, name, table, sql in objects:
        if (
            kind == "trigger"
            and table in _SOURCES
            and (
                name not in _MIGRATION_TRIGGERS
                or sql is None
                or _normalized(sql) != _normalized(_MIGRATION_TRIGGERS[name])
            )
        ):
            raise _fail(PurgeRefusal.SCHEMA_DRIFT)
    return states, rp._source_fingerprint(objects, unknown, components)


def _check_effects(database: sqlite3.Connection, case_token: str) -> None:
    """Supplement caller facts with available authoritative local lifecycle state."""
    columns = dict(rp._table_info(database, "pb_cases"))
    for column, allowed in (
        ("letter_state", {"DELIVERED", "CLOSED_RETURNED", "STOPPED_NO_CHARGE"}),
        ("payment_state", {"NO_CHARGE", "CAPTURED", "WRITTEN_OFF", "RELEASED", "REFUNDED"}),
    ):
        if column in columns:
            row = database.execute(
                f"SELECT {column} FROM pb_cases WHERE case_token = ?", (case_token,)
            ).fetchone()
            if row is None or row[0] not in allowed:
                raise _fail(PurgeRefusal.UNRESOLVED_EFFECTS)
    # Outstanding effect records override a falsely clean caller blocker record.
    if (
        rp._table_info(database, "pb_pending_effects")
        and database.execute(
            "SELECT 1 FROM pb_pending_effects WHERE case_token = ? "
            "AND completed_at IS NULL LIMIT 1",
            (case_token,),
        ).fetchone()
    ):
        raise _fail(PurgeRefusal.UNRESOLVED_EFFECTS)
    for table in ("pb_submission_operations", "pb_payment_operations"):
        if (
            rp._table_info(database, table)
            and database.execute(
                f"SELECT 1 FROM {table} WHERE case_token = ? AND completed_at IS NULL LIMIT 1",
                (case_token,),
            ).fetchone()
        ):
            raise _fail(PurgeRefusal.UNRESOLVED_EFFECTS)
    try:
        if rp._table_info(database, "pb_submission_cursors"):
            sql = "SELECT outcome_json FROM pb_submission_cursors WHERE case_token = ?"
            params: tuple[str, ...]
            if "attempt" in columns:
                sql += " AND attempt = (SELECT attempt FROM pb_cases WHERE case_token = ?)"
                params = (case_token, case_token)
            else:
                params = (case_token,)
            for (payload,) in database.execute(sql, params):
                if isinstance(decode_outcome(payload), Uncertain):
                    raise _fail(PurgeRefusal.UNRESOLVED_EFFECTS)
        if rp._table_info(database, "pb_payment_cursor"):
            for (payload,) in database.execute(
                "SELECT payload FROM pb_payment_cursor WHERE case_token = ?", (case_token,)
            ):
                cursor = decode_cursor(payload)
                if not cursor.done or cursor.outcome.status in {
                    "uncertain",
                    "transient",
                    "requires_capture",
                }:
                    raise _fail(PurgeRefusal.UNRESOLVED_EFFECTS)
    except (TypeError, ValueError, KeyError):
        raise _fail(PurgeRefusal.UNRESOLVED_EFFECTS) from None
    if (
        database.execute(
            "SELECT 1 FROM pb_draft_attempts WHERE case_token = ? AND outcome IS NULL LIMIT 1",
            (case_token,),
        ).fetchone()
        or database.execute(
            "SELECT 1 FROM pb_ticket_intake_operations o JOIN pb_intake_records i "
            "ON i.intake_id = o.intake_id WHERE i.case_token = ? AND o.status = 'pending' LIMIT 1",
            (case_token,),
        ).fetchone()
    ):
        raise _fail(PurgeRefusal.UNRESOLVED_EFFECTS)


def _snapshot(
    database: sqlite3.Connection, case_token: str, at: datetime, blockers: UnresolvedBlockers | None
) -> rs.CaseRetentionSnapshot:
    snapshot = rs.case_snapshot(database, case_token)
    if any(hold.active for hold in snapshot.holds):
        # Financial/unknown holds also protect overlapping evidence; do not guess scope.
        raise _fail(PurgeRefusal.HOLD_ACTIVE)
    decision = _decision(snapshot, at, blockers)
    if not decision.eligible:
        if not isinstance(blockers, UnresolvedBlockers) or any(
            dataclasses.asdict(blockers).values()
        ):
            raise _fail(PurgeRefusal.UNRESOLVED_EFFECTS)
        raise _fail(PurgeRefusal.PLAN_NOT_ELIGIBLE)
    _check_effects(database, case_token)
    return snapshot


def _decision(
    snapshot: rs.CaseRetentionSnapshot, at: datetime, blockers: UnresolvedBlockers | None
) -> RetentionDecision:
    return evaluate_service_retention(
        None if snapshot.policy is None else snapshot.policy.to_policy(),
        scope=DataScope.SERVICE_DATA,
        evaluated_at=at,
        anchors=() if snapshot.closure is None else (snapshot.closure.to_anchor(),),
        holds=tuple(hold.to_hold() for hold in snapshot.holds),
        blockers=blockers,
    )


def _manifest_digest(
    manifest: RetentionPlanManifest,
    snapshot: rs.CaseRetentionSnapshot,
    planned_at: datetime,
    max_plan_rows: int,
) -> str:
    """Exact planner-v1 canonical digest, including original evaluation instant/bound."""
    return rp._digest(
        {
            "format_version": rp.FORMAT_VERSION,
            "evaluated_at": rp._timestamp_text(planned_at),
            "source_fingerprint": manifest.source_fingerprint,
            "binding": manifest.binding_digest,
            "policy": rp._policy_digest(snapshot),
            "closure": rp._closure_digest(snapshot),
            "holds": rp._holds_digest(snapshot),
            "blockers": dataclasses.asdict(UnresolvedBlockers()),
            "decision": _decision(snapshot, planned_at, UnresolvedBlockers()).to_dict(),
            "blocked_reasons": [],
            "partial": False,
            "max_plan_rows": max_plan_rows,
            "targets": [
                [
                    t.category.value,
                    t.table,
                    t.row_key,
                    t.column,
                    t.content_sha256,
                    t.content_bytes,
                    t.disposition.value,
                    t.reason,
                ]
                for t in sorted(
                    manifest.targets, key=lambda t: (t.category.value, t.table, t.row_key, t.column)
                )
            ],
            "blobs": [
                [b.owner_dir, b.ref, b.disposition.value, b.reason, b.reference_count]
                for b in manifest.blobs
            ],
            "retained_scopes": [
                [s.scope, s.table, s.present, s.row_count] for s in manifest.retained_scopes
            ],
        }
    )


def plan_retention_purge(
    database: sqlite3.Connection,
    *,
    case_token: str,
    evaluated_at: datetime,
    blockers: UnresolvedBlockers | None,
    blob_store: BlobDeleteStore,
    max_plan_rows: int = rp.DEFAULT_MAX_PLAN_ROWS,
) -> RetentionPlanManifest:
    """Plan a fresh case after this component has installed its fence/receipt schema.

    Returns the same planner-v1 manifest and digest as the original planner on a fresh
    database. Unlike that read-only inventory primitive, this entry point understands and
    validates the executor's schema extension and refuses ineligible cases. Once fenced,
    a case must resume its original manifest; planning cannot reset a partial purge.
    """
    if type(max_plan_rows) is not int or max_plan_rows < 1:
        raise _fail(PurgeRefusal.INVALID_INPUT)
    with _transaction(database, write=False):
        assert_service_access(database, case_token=case_token)
        snapshot = _snapshot(database, case_token, evaluated_at, blockers)
        states, fingerprint = _source_schema(database)
        binding, reason = rp._resolve_binding(database, case_token, states)
        if binding is None or reason is not None:
            raise _fail(PurgeRefusal.REVISION_DRIFT)
        collector = rp._Collector(max_plan_rows)
        try:
            rp._Inventory(database, binding, states, False, collector, rp.DEFAULT_BATCH_SIZE).run()
        except (rp._PlanBoundReached, rp._PageMalformed):
            raise _fail(PurgeRefusal.PLAN_NOT_ELIGIBLE) from None
        targets = tuple(collector.targets)
        if any(
            t.disposition
            not in {rp.TargetDisposition.CANDIDATE, rp.TargetDisposition.REQUIRES_MIGRATION}
            for t in targets
        ):
            raise _fail(PurgeRefusal.PLAN_NOT_ELIGIBLE)
        blobs = rp._blob_targets(database, binding, collector.refs, False, False, blob_store)
        kinds = dict(database.execute("SELECT name, type FROM sqlite_master WHERE type = 'table'"))
        retained = rp._retained_scopes(database, kinds, binding)
        summary = rp._summarise(
            blocked=(),
            partial=False,
            states=states,
            unknown=(),
            targets=targets,
            blobs=blobs,
            retained=retained,
        )
        manifest = RetentionPlanManifest(
            format_version=rp.FORMAT_VERSION,
            case_token=case_token,
            workspace_token=binding.workspace_token,
            intake_id=binding.intake_id,
            owner_dir=_hash(binding.owner),
            plan_digest="",
            source_fingerprint=fingerprint,
            binding_digest=rp._binding_digest(binding),
            policy_revision_digest=rp._policy_digest(snapshot),
            closure_revision_digest=rp._closure_digest(snapshot),
            hold_revisions_digest=rp._holds_digest(snapshot),
            decision_digest=rp._digest(_decision(snapshot, evaluated_at, blockers).to_dict()),
            partial=False,
            source_statuses=tuple(
                rp.SourceStatus(table=t, state=s) for t, s in sorted(states.items())
            ),
            targets=targets,
            blobs=blobs,
            retained_scopes=retained,
            summary=summary,
        )
        return dataclasses.replace(
            manifest, plan_digest=_manifest_digest(manifest, snapshot, evaluated_at, max_plan_rows)
        )


def _replacement(target: rp.PlanTarget) -> str | None:
    if target.column in {"result_json", "agency_json"}:
        return None
    return REDACTED_JSON if target.column.endswith("_json") else ""


def _done(
    database: sqlite3.Connection, case_digest: str
) -> dict[int, tuple[str, str | None, str | None]]:
    return {
        row[0]: (row[1], row[2], row[3])
        for row in database.execute(
            "SELECT sequence, category, before_digest, after_digest "
            "FROM pb_purge_targets WHERE case_digest = ?",
            (case_digest,),
        )
    }


def _verify(
    database: sqlite3.Connection,
    manifest: RetentionPlanManifest,
    *,
    planned_at: datetime,
    evaluated_at: datetime,
    blockers: UnresolvedBlockers | None,
    blob_store: BlobDeleteStore,
    max_plan_rows: int,
) -> tuple[rp._Binding, dict[int, tuple[str, str | None, str | None]]]:
    if (
        manifest.format_version != rp.FORMAT_VERSION
        or manifest.partial
        or not manifest.summary.eligible
        or manifest.summary.blocked_reasons
        or any(
            target.disposition
            not in {rp.TargetDisposition.CANDIDATE, rp.TargetDisposition.REQUIRES_MIGRATION}
            for target in manifest.targets
        )
    ):
        raise _fail(PurgeRefusal.PLAN_NOT_ELIGIBLE)
    snapshot = _snapshot(database, manifest.case_token, evaluated_at, blockers)
    if (rp._policy_digest(snapshot), rp._closure_digest(snapshot), rp._holds_digest(snapshot)) != (
        manifest.policy_revision_digest,
        manifest.closure_revision_digest,
        manifest.hold_revisions_digest,
    ):
        raise _fail(PurgeRefusal.REVISION_DRIFT)
    if _manifest_digest(manifest, snapshot, planned_at, max_plan_rows) != manifest.plan_digest:
        raise _fail(PurgeRefusal.PLAN_DIGEST_MISMATCH)
    states, fingerprint = _source_schema(database)
    if fingerprint != manifest.source_fingerprint:
        raise _fail(PurgeRefusal.SCHEMA_DRIFT)
    binding, reason = rp._resolve_binding(database, manifest.case_token, states)
    if (
        binding is None
        or reason is not None
        or rp._binding_digest(binding) != manifest.binding_digest
    ):
        raise _fail(PurgeRefusal.REVISION_DRIFT)
    if (manifest.workspace_token, manifest.intake_id, manifest.owner_dir) != (
        binding.workspace_token,
        binding.intake_id,
        _hash(binding.owner),
    ):
        raise _fail(PurgeRefusal.PLAN_DIGEST_MISMATCH)
    case_digest = _hash(manifest.case_token)
    done: dict[int, tuple[str, str | None, str | None]] = {}
    existing = None
    if _schema_ready(database):
        existing = database.execute(
            "SELECT plan_digest, manifest_digest, schema_version FROM pb_purge_cases "
            "WHERE case_digest = ?",
            (case_digest,),
        ).fetchone()
    if existing is not None:
        if existing != (
            manifest.plan_digest,
            rp._digest(dataclasses.asdict(manifest)),
            SCHEMA_VERSION,
        ):
            raise _fail(PurgeRefusal.PLAN_DIGEST_MISMATCH)
        done = _done(database, case_digest)
        if any(type(i) is not int or not 0 <= i < len(manifest.targets) for i in done):
            raise _fail(PurgeRefusal.CONTENT_DRIFT)
    collector = rp._Collector(max_plan_rows)
    try:
        rp._Inventory(database, binding, states, False, collector, rp.DEFAULT_BATCH_SIZE).run()
    except (rp._PlanBoundReached, rp._PageMalformed):
        raise _fail(PurgeRefusal.CONTENT_DRIFT) from None
    expected = list(manifest.targets)
    for i, saved in done.items():
        target = manifest.targets[i]
        digest, size, _valid = rp._content(_replacement(target), nullable=True)
        if saved != (target.category.value, target.content_sha256, digest):
            raise _fail(PurgeRefusal.CONTENT_DRIFT)
        expected[i] = dataclasses.replace(target, content_sha256=digest, content_bytes=size)
    expected = [
        target
        for i, target in enumerate(expected)
        if not (i in done and target.category is rp.TargetCategory.APPROVAL_MANIFEST)
    ]
    if tuple(collector.targets) != tuple(expected):
        raise _fail(PurgeRefusal.CONTENT_DRIFT)
    if existing is None:
        blobs = rp._blob_targets(database, binding, collector.refs, False, False, blob_store)
        if blobs != manifest.blobs:
            raise _fail(PurgeRefusal.CONTENT_DRIFT)
        # All non-digest dataclass fields must agree too; none is trusted as authorization.
        statuses = tuple(rp.SourceStatus(table=t, state=s) for t, s in sorted(states.items()))
        summary = rp._summarise(
            blocked=(),
            partial=False,
            states=states,
            unknown=(),
            targets=manifest.targets,
            blobs=blobs,
            retained=manifest.retained_scopes,
        )
        if (
            manifest.source_statuses != statuses
            or manifest.summary != summary
            or manifest.decision_digest
            != rp._digest(_decision(snapshot, planned_at, UnresolvedBlockers()).to_dict())
        ):
            raise _fail(PurgeRefusal.PLAN_DIGEST_MISMATCH)
    return binding, done


def _locator(target: rp.PlanTarget, binding: rp._Binding) -> tuple[str, tuple[object, ...]]:
    if target.category is rp.TargetCategory.APPROVAL_MANIFEST:
        if target.table != "pb_approval_manifests" or target.column != "manifest_json":
            raise _fail(PurgeRefusal.PLAN_DIGEST_MISMATCH)
        _case, attempt = target.row_key.rsplit("#", 1)
        return "case_token = ? AND attempt = ?", (binding.case_token, int(attempt))
    column, fence_column, payloads = _SOURCES[target.table]
    if target.column not in payloads:
        raise _fail(PurgeRefusal.PLAN_DIGEST_MISMATCH)
    value = getattr(binding, fence_column)
    sql, params = f"{column} = ?", (value,)
    if target.table == "pb_draft_attempts":
        _case, generation, attempt = target.row_key.rsplit("#", 2)
        return sql + " AND generation = ? AND attempt = ?", (*params, int(generation), int(attempt))
    if target.table == "pb_beta_feedback_notices":
        return sql + " AND notice_id = ?", (*params, target.row_key)
    if target.table == "pb_ticket_intake_operations":
        return sql + " AND request_id = ?", (*params, target.row_key)
    if target.table == "pb_beta_contact_targets":
        target_id, revision = target.row_key.rsplit("#", 1)
        return sql + " AND target_id = ? AND revision = ?", (*params, target_id, int(revision))
    parts = target.row_key.rsplit("#", 2)
    revision = (
        parts[-2]
        if target.table in {"pb_intake_records", "pb_intake_drafts", "pb_intake_research"}
        else parts[-1]
    )
    return sql + " AND revision = ?", (*params, int(revision))


@contextlib.contextmanager
def _migration(database: sqlite3.Connection, tables: set[str]) -> Iterator[None]:
    """Transactional DDL: crash/exception rollback restores both triggers and payloads."""
    originals: list[tuple[str, str]] = []
    for table in sorted(tables):
        for name, sql in database.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            (table,),
        ).fetchall():
            # _source_schema has already validated every original and every fence.
            originals.append((name, sql))
            database.execute(f"DROP TRIGGER {rp._quote(name)}")
    yield
    for _name, sql in originals:
        database.execute(sql)


def _redact(
    database: sqlite3.Connection,
    manifest: RetentionPlanManifest,
    binding: rp._Binding,
    indexes: list[int],
) -> None:
    with _migration(database, {manifest.targets[i].table for i in indexes}):
        for i in indexes:
            target = manifest.targets[i]
            predicate, params = _locator(target, binding)
            replacement = _replacement(target)
            if target.category is rp.TargetCategory.APPROVAL_MANIFEST:
                cursor = database.execute(
                    f"DELETE FROM {rp._quote(target.table)} WHERE {predicate}", params
                )
            else:
                cursor = database.execute(
                    f"UPDATE {rp._quote(target.table)} SET {rp._quote(target.column)} = ? "
                    f"WHERE {predicate}",
                    (replacement, *params),
                )
            if cursor.rowcount != 1:
                raise _fail(PurgeRefusal.CONTENT_DRIFT)
            digest, _size, _valid = rp._content(replacement, nullable=True)
            database.execute(
                "INSERT INTO pb_purge_targets VALUES (?, ?, ?, ?, ?)",
                (
                    _hash(manifest.case_token),
                    i,
                    target.category.value,
                    target.content_sha256,
                    digest,
                ),
            )


def _surviving_blob(database: sqlite3.Connection, binding: rp._Binding, ref: str) -> str | None:
    """Conservative scan also covers restricted originals and nullable JSON columns.

    Across-owner matches are retained too. A false positive retains extra data rather than
    deleting a shared object. No assumption that the dry-run planner knows every reference.
    """
    tables = database.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    for (table,) in tables:
        if table.startswith(("sqlite_", "pb_purge_")):
            continue
        columns = [
            name for name, kind in rp._table_info(database, table) if kind in {"TEXT", "BLOB"}
        ]
        if not columns:
            continue
        predicates = " OR ".join(f"instr(CAST({rp._quote(c)} AS TEXT), ?) > 0" for c in columns)
        sql = f"SELECT 1 FROM {rp._quote(table)} WHERE ({predicates})"
        params: tuple[object, ...] = (ref,) * len(columns)
        category = "restricted"
        if table in _SOURCES:
            column, fence_column, _payloads = _SOURCES[table]
            sql += f" AND {rp._quote(column)} IS NOT ?"
            params += (getattr(binding, fence_column),)
            category = "shared"
        if database.execute(sql + " LIMIT 1", params).fetchone():
            return category
    return None


def _delete_blob(
    database: sqlite3.Connection, binding: rp._Binding, blob: rp.BlobTarget, store: BlobDeleteStore
) -> str:
    if blob.disposition is rp.BlobDisposition.RETAINED_SHARED:
        return "shared"
    if blob.disposition not in {rp.BlobDisposition.CANDIDATE, rp.BlobDisposition.MISSING_OR_ORPHAN}:
        return "protected"
    surviving = _surviving_blob(database, binding, blob.ref)
    if surviving is not None:
        return surviving
    try:
        data = store.get(binding.owner, blob.ref)
        if data is None:
            return "absent"
        if (
            blob.disposition is not rp.BlobDisposition.CANDIDATE
            or not isinstance(data, bytes)
            or "photo_" + hashlib.sha256(data).hexdigest() != blob.ref
        ):
            return "protected"
        store.delete(binding.owner, blob.ref)
        return "deleted" if store.get(binding.owner, blob.ref) is None else "failed"
    except Exception:
        # Exception messages can contain customer text or paths. Never persist or expose them.
        return "failed"


def _receipt(database: sqlite3.Connection, manifest: RetentionPlanManifest) -> PurgeReceipt:
    case_digest = _hash(manifest.case_token)
    categories = tuple(
        (str(row[0]), int(row[1]))
        for row in database.execute(
            "SELECT category, COUNT(*) FROM pb_purge_targets WHERE case_digest = ? "
            "GROUP BY category ORDER BY category",
            (case_digest,),
        )
    )
    blobs = tuple(
        (str(row[0]), int(row[1]))
        for row in database.execute(
            "SELECT outcome, COUNT(*) FROM pb_purge_blobs WHERE case_digest = ? "
            "GROUP BY outcome ORDER BY outcome",
            (case_digest,),
        )
    )
    done = sum(count for _category, count in categories)
    if done < len(manifest.targets):
        status = "database_pending"
    elif sum(count for _category, count in blobs) < len(manifest.blobs):
        status = "blobs_pending"
    elif any(category in {"failed", "protected"} and count for category, count in blobs):
        status = "partial_blob_failure"
    else:
        status = "scoped_complete"
    return PurgeReceipt(
        manifest.plan_digest,
        status,
        len(manifest.targets),
        done,
        len(manifest.blobs),
        blobs,
        categories,
    )


def execute_retention_purge(
    database: sqlite3.Connection,
    manifest: RetentionPlanManifest,
    *,
    synthetic: bool,
    planned_at: datetime,
    evaluated_at: datetime,
    blockers: UnresolvedBlockers | None,
    blob_store: BlobDeleteStore,
    batch_size: int = rp.DEFAULT_BATCH_SIZE,
    max_plan_rows: int = rp.DEFAULT_MAX_PLAN_ROWS,
) -> PurgeReceipt:
    """Advance at most batch_size payloads/objects; repeat explicitly until complete.

    A durable fence is committed before the first destructive batch. Every invocation
    validates the manifest, source/retention revisions, remaining content and current
    blockers while holding SQLite's write lock. Already processed content must still equal
    its tombstone. Caller transactions are never committed or rolled back. A crash after
    blob deletion but before SQL commit is reconciled as ``absent`` on the next call.
    ``synthetic=True`` is a caller attestation, not production deletion authorization.
    """
    if synthetic is not True:
        raise _fail(PurgeRefusal.SYNTHETIC_ONLY)
    if (
        type(batch_size) is not int
        or not 1 <= batch_size <= rp.MAX_BATCH_SIZE
        or type(max_plan_rows) is not int
        or max_plan_rows < 1
        or not isinstance(manifest, RetentionPlanManifest)
        or (blockers is not None and not isinstance(blockers, UnresolvedBlockers))
    ):
        raise _fail(PurgeRefusal.INVALID_INPUT)
    with _transaction(database, write=True):
        _binding, _done_targets = _verify(
            database,
            manifest,
            planned_at=planned_at,
            evaluated_at=evaluated_at,
            blockers=blockers,
            blob_store=blob_store,
            max_plan_rows=max_plan_rows,
        )
        if not _schema_ready(database):
            for ddl in (*_DDL.values(), *_FENCES.values()):
                database.execute(ddl)
        if (
            database.execute(
                "SELECT 1 FROM pb_purge_cases WHERE case_digest = ?", (_hash(manifest.case_token),)
            ).fetchone()
            is None
        ):
            database.execute(
                "INSERT INTO pb_purge_cases VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _hash(manifest.case_token),
                    manifest.case_token,
                    manifest.workspace_token,
                    manifest.intake_id,
                    manifest.plan_digest,
                    rp._digest(dataclasses.asdict(manifest)),
                    SCHEMA_VERSION,
                ),
            )
    with _transaction(database, write=True):
        binding, done = _verify(
            database,
            manifest,
            planned_at=planned_at,
            evaluated_at=evaluated_at,
            blockers=blockers,
            blob_store=blob_store,
            max_plan_rows=max_plan_rows,
        )
        indexes = [i for i in range(len(manifest.targets)) if i not in done][:batch_size]
        _redact(database, manifest, binding, indexes)
        _ensure_approval_manifest_fence(database)
        remaining = batch_size - len(indexes)
        if len(done) + len(indexes) == len(manifest.targets) and remaining:
            previous = dict(
                database.execute(
                    "SELECT blob_digest, outcome FROM pb_purge_blobs WHERE case_digest = ?",
                    (_hash(manifest.case_token),),
                )
            )
            pending = [b for b in manifest.blobs if _hash(b.ref) not in previous]
            pending += [b for b in manifest.blobs if previous.get(_hash(b.ref)) == "failed"]
            for blob in pending[:remaining]:
                outcome = _delete_blob(database, binding, blob, blob_store)
                database.execute(
                    "INSERT INTO pb_purge_blobs VALUES (?, ?, ?) ON CONFLICT "
                    "(case_digest, blob_digest) DO UPDATE SET outcome = excluded.outcome",
                    (_hash(manifest.case_token), _hash(blob.ref), outcome),
                )
        return _receipt(database, manifest)


def assert_service_access(
    database: sqlite3.Connection,
    *,
    case_token: str | None = None,
    workspace_token: str | None = None,
    intake_id: str | None = None,
) -> None:
    """Fence normal customer/workspace reads, including a partially completed purge.

    Use guarded_service_read to keep the check and the read in the same SQLite snapshot.
    A supplied identifier that is tombstoned always refuses, regardless of other arguments.
    """
    supplied = [
        (column, value)
        for column, value in (
            ("case_token", case_token),
            ("workspace_token", workspace_token),
            ("intake_id", intake_id),
        )
        if value is not None
    ]
    if not supplied or any(type(value) is not str or not value for _column, value in supplied):
        raise _fail(PurgeRefusal.INVALID_INPUT)
    if _schema_ready(database):
        predicates = " OR ".join(f"{column} = ?" for column, _value in supplied)
        if database.execute(
            f"SELECT 1 FROM pb_purge_cases WHERE {predicates} LIMIT 1",
            tuple(value for _column, value in supplied),
        ).fetchone():
            raise _fail(PurgeRefusal.ACCESS_FENCED)


def guarded_service_read[T](
    database: sqlite3.Connection,
    reader: Callable[[], T],
    *,
    case_token: str | None = None,
    workspace_token: str | None = None,
    intake_id: str | None = None,
) -> T:
    with _transaction(database, write=False):
        assert_service_access(
            database, case_token=case_token, workspace_token=workspace_token, intake_id=intake_id
        )
        return reader()


def validate_retention_restore(
    backup: sqlite3.Connection,
    authority: sqlite3.Connection,
    *,
    evaluated_at: datetime,
) -> None:
    """Refuse an entire restore containing expired/tombstoned ordinary service copies.

    Both connections must be quiescent. Authority must be the independently retained current
    database, including its tombstones, not a second connection to the old backup. Every
    backed-up case must exist in authority; missing closure/policy facts fail closed. A hold
    retains evidence separately, never licenses restoration into the normal service path.
    The caller must keep the authority snapshot valid until promoting a validated restore.
    """
    with retention_restore_guard(backup, authority, evaluated_at=evaluated_at):
        pass


def _validate_restore(
    backup: sqlite3.Connection, authority: sqlite3.Connection, evaluated_at: datetime
) -> None:
    source_states, _ = _source_schema(backup)
    authority_states, _ = _source_schema(authority)
    known: dict[str, set[str]] = {"case_token": set(), "workspace_token": set(), "intake_id": set()}
    cursor = backup.execute("SELECT case_token, workspace_token FROM pb_cases ORDER BY case_token")
    while rows := cursor.fetchmany(rp.DEFAULT_BATCH_SIZE):
        for case_token, workspace_token in rows:
            source_binding, reason = rp._resolve_binding(backup, case_token, source_states)
            current_binding, current_reason = rp._resolve_binding(
                authority, case_token, authority_states
            )
            if source_binding is None or current_binding is None or reason or current_reason:
                raise _fail(PurgeRefusal.RESTORE_EXCLUDED)
            if (source_binding.workspace_token, source_binding.intake_id, source_binding.owner) != (
                current_binding.workspace_token,
                current_binding.intake_id,
                current_binding.owner,
            ):
                raise _fail(PurgeRefusal.RESTORE_EXCLUDED)
            for field, values in known.items():
                values.add(getattr(source_binding, field))
            for database in (authority, backup):
                try:
                    assert_service_access(
                        database,
                        case_token=case_token,
                        workspace_token=workspace_token,
                        intake_id=source_binding.intake_id,
                    )
                    snapshot = rs.case_snapshot(database, case_token)
                except (RetentionPurgeRefused, rs.RetentionStateInputError):
                    raise _fail(PurgeRefusal.RESTORE_EXCLUDED) from None
                # Holds protect evidence separately; they never extend normal service access.
                decision = _decision(
                    dataclasses.replace(snapshot, holds=()), evaluated_at, UnresolvedBlockers()
                )
                if (
                    decision.boundary is None
                    or decision.evaluated_at is None
                    or decision.evaluated_at >= decision.boundary
                ):
                    raise _fail(PurgeRefusal.RESTORE_EXCLUDED)
    # A backup omitting pb_cases rows must not smuggle expired orphan payloads back in.
    for table, (column, field, _payloads) in _SOURCES.items():
        cursor = backup.execute(f"SELECT DISTINCT {column} FROM {table}")
        while rows := cursor.fetchmany(rp.DEFAULT_BATCH_SIZE):
            if any(row[0] not in known[field] for row in rows):
                raise _fail(PurgeRefusal.RESTORE_EXCLUDED)
    if _has_approval_manifests(backup):
        cursor = backup.execute("SELECT DISTINCT case_token FROM pb_approval_manifests")
        while rows := cursor.fetchmany(rp.DEFAULT_BATCH_SIZE):
            if any(row[0] not in known["case_token"] for row in rows):
                raise _fail(PurgeRefusal.RESTORE_EXCLUDED)


@contextlib.contextmanager
def retention_restore_guard(
    backup: sqlite3.Connection, authority: sqlite3.Connection, *, evaluated_at: datetime
) -> Iterator[None]:
    """Validate and hold both snapshots while the caller copies an approved synthetic restore.

    The independent authority and its tombstones must survive the restore. The caller may
    copy only the locked backup snapshot to a separate target inside this context; replacing
    authority with a backup would discard the very exclusion ledger that protects restores.
    """
    if backup is authority or backup.in_transaction or authority.in_transaction:
        raise _fail(PurgeRefusal.CALLER_TRANSACTION_ACTIVE)
    backup_file = backup.execute("PRAGMA database_list").fetchone()[2]
    authority_file = authority.execute("PRAGMA database_list").fetchone()[2]
    if backup_file and backup_file == authority_file:
        raise _fail(PurgeRefusal.RESTORE_EXCLUDED)
    with _transaction(authority, write=True), _transaction(backup, write=False):
        _validate_restore(backup, authority, evaluated_at)
        yield
