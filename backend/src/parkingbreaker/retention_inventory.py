"""Read-only retention inventory for an operator-supplied SQLite snapshot tree.

The caller supplies a private, already completed standalone snapshot and the explicit
relative paths of the databases inside it. This module only counts metadata: it never
mutates, migrates, deletes or adopts a retention policy. Unknown material is conservatively
classified as protected, and every count is an inventory count rather than permission to
delete. It does not import application stores, because store construction can migrate.
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import os
import posixpath
import re
import sqlite3
import stat
from pathlib import Path
from typing import Final

SCHEMA_VERSION: Final = 1
INVENTORY_SCOPE: Final = "supplied_snapshot_root"
_MAX_DEPTH: Final = 64
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_PHOTO: Final = re.compile(r"photo_[0-9a-f]{64}")
_PDF: Final = re.compile(r"[0-9a-f]{64}\.pdf")
_SQLITE_SUFFIXES: Final = (".sqlite", ".sqlite3", ".db", ".db3")
_PERMITTED_DATABASE_BASENAME: Final = "parkingbreaker.sqlite"
_TEMP_SUFFIXES: Final = (
    ".tmp",
    ".temp",
    ".part",
    ".partial",
    ".bak",
    ".swp",
    ".swx",
    ".orig",
    ".rej",
    ".copy",
    ".crdownload",
    ".download",
    ".candidate",
)
_CONTROL_NAMES: Final = frozenset(
    {
        "COMPLETE",
        "COMPLETE.json",
        "INCOMPLETE",
        "MANIFEST.json",
        "SNAPSHOT.json",
        "SNAPSHOT_ACTIVE",
        "SNAPSHOT_INCOMPLETE",
        "backup_manifest.json",
        "manifest.json",
        "snapshot.json",
        "snapshot_manifest.json",
    }
)
_INCOMPLETE_MARKERS: Final = frozenset(
    {
        "INCOMPLETE",
        "SNAPSHOT_ACTIVE",
        "SNAPSHOT_INCOMPLETE",
        "BACKUP_INCOMPLETE",
        "RESTORE_INCOMPLETE",
    }
)


class InventoryRefused(enum.Enum):
    """Stable refusal reasons. Strings never contain a path or an exception message."""

    ROOT_NOT_ABSOLUTE = "root_not_absolute"
    INVALID_ROOT = "invalid_root"
    SYMLINK_IN_PATH = "symlink_in_path"
    INVALID_DATABASE_PATH = "invalid_database_path"
    DUPLICATE_DATABASE_PATH = "duplicate_database_path"
    MISSING_DATABASE = "missing_database"
    DATABASE_NOT_REGULAR = "database_not_regular"
    HARDLINKED_ENTRY = "hardlinked_entry"
    UNSAFE_ENTRY = "unsafe_entry"
    DEVICE_BOUNDARY = "device_boundary"
    DEPTH_EXCEEDED = "depth_exceeded"
    READ_FAILED = "read_failed"
    CONCURRENT_MUTATION = "concurrent_mutation"
    DATABASE_DESCRIPTOR_MISMATCH = "database_descriptor_mismatch"
    MALFORMED_DATABASE = "malformed_database"
    SQLITE_UNAVAILABLE = "sqlite_unavailable"
    IDENTIFIER_UNSAFE = "identifier_unsafe"
    UNSUPPORTED_DATABASE_BASENAME = "unsupported_database_basename"


class InventoryRefusal(Exception):
    """A refusal carrying only a stable reason enum member."""

    def __init__(self, reason: InventoryRefused) -> None:
        super().__init__(reason.value)
        self.reason = reason


class FileClass(enum.StrEnum):
    DECLARED_SQLITE_DATABASE = "declared_sqlite_database"
    UNINSPECTED_SQLITE_COPY_CANDIDATE = "uninspected_sqlite_copy_candidate"
    SQLITE_WAL = "sqlite_wal"
    SQLITE_SHM = "sqlite_shm"
    SQLITE_ROLLBACK_JOURNAL = "sqlite_rollback_journal"
    OWNER_SCOPED_PHOTO_BLOB = "owner_scoped_photo_blob"
    EXPORTED_FROZEN_PDF = "exported_frozen_pdf"
    TEMPORARY_COPY_CANDIDATE = "temporary_copy_candidate"
    BACKUP_CONTROL_METADATA = "backup_control_metadata"
    PROTECTED_UNKNOWN_COPY = "protected_unknown_copy"


class TableClass(enum.StrEnum):
    PROTECTED_CUSTOMER_CONTENT = "protected_customer_content"
    RESTRICTED_FINANCIAL_PROVIDER_EVIDENCE = "restricted_financial_provider_evidence"
    RESTRICTED_IDENTITY_OPERATIONAL = "restricted_identity_operational"
    STRUCTURAL_METADATA = "structural_metadata"
    PROTECTED_UNKNOWN = "protected_unknown"


_PROTECTED_CUSTOMER_CONTENT: Final = frozenset(
    {
        "pb_workspaces",
        "pb_workspace_history",
        "pb_intake_owners",
        "pb_intake_records",
        "pb_intake_drafts",
        "pb_intake_research",
        "pb_draft_attempts",
        "pb_mail_attempts",
        "pb_required_copies",
        "pb_address_revisions",
        "pb_address_consents",
        "pb_address_decisions",
        "pb_mail_destination_bindings",
        "pb_beta_feedback_notices",
    }
)
_RESTRICTED_FINANCIAL_PROVIDER_EVIDENCE: Final = frozenset(
    {
        "pb_intake_authorization_attempts",
        "pb_payment_cursor",
        "pb_payment_operations",
        "pb_webhooks",
        "pb_webhook_backlog",
        "pb_webhook_unmatched",
        "pb_webhook_final_events",
        "pb_return_remedies",
        "pb_submission_cursors",
        "pb_submission_operations",
    }
)
_RESTRICTED_IDENTITY_OPERATIONAL: Final = frozenset(
    {
        "pb_identity_accounts",
        "pb_identity_links",
        "pb_identity_sessions",
        "pb_cases",
        "pb_case_history",
        "pb_pending_effects",
        "pb_draft_slots",
        "pb_draft_runs",
        "pb_ai_calls",
        "pb_ai_journal",
        "pb_ai_spend",
        "pb_case_holds",
        "pb_case_hold_commands",
        "pb_case_hold_busy",
        "pb_address_locks",
        "pb_access_email_calls",
        "pb_notification_access_links",
        "pb_tracking_email_mark",
        "pb_tracking_emails",
        "pb_price_visitors",
        "pb_price_credentials",
        "pb_price_scopes",
        "pb_beta_campaigns",
        "pb_beta_jurisdiction_aliases",
        "pb_beta_candidates",
        "pb_beta_batches",
        "pb_beta_entitlements",
        "pb_journey_links",
        "pb_journey_capture",
        "pb_journey_event_context",
        "pb_journey_events",
        "pb_ticket_ocr_observations",
        "pb_ticket_review_sessions",
        "pb_ticket_review_observations",
        "pb_support_codes",
        "pb_support_messages",
        "pb_support_confirmation_emails",
    }
)
_STRUCTURAL_METADATA: Final = frozenset({"pb_store_versions"})
_KNOWN_TABLES: Final[dict[str, TableClass]] = {
    **dict.fromkeys(_PROTECTED_CUSTOMER_CONTENT, TableClass.PROTECTED_CUSTOMER_CONTENT),
    **dict.fromkeys(
        _RESTRICTED_FINANCIAL_PROVIDER_EVIDENCE, TableClass.RESTRICTED_FINANCIAL_PROVIDER_EVIDENCE
    ),
    **dict.fromkeys(_RESTRICTED_IDENTITY_OPERATIONAL, TableClass.RESTRICTED_IDENTITY_OPERATIONAL),
    **dict.fromkeys(_STRUCTURAL_METADATA, TableClass.STRUCTURAL_METADATA),
}


@dataclasses.dataclass(frozen=True)
class FileClassCount:
    file_class: FileClass
    file_count: int
    logical_bytes: int


@dataclasses.dataclass(frozen=True)
class TableClassCount:
    table_class: TableClass
    table_count: int
    row_count: int
    unavailable_table_count: int


@dataclasses.dataclass(frozen=True)
class InventoryReport:
    schema_version: int
    inventory_scope: str
    file_classes: tuple[FileClassCount, ...]
    table_classes: tuple[TableClassCount, ...]
    directory_count: int
    file_count: int
    logical_bytes: int
    declared_database_count: int
    inspected_database_count: int
    uninspected_database_count: int
    unknown_table_count: int
    unknown_schema_object_count: int
    row_count_unavailable_count: int
    safety_status: str
    reason_codes: tuple[str, ...]
    inventory_complete: bool
    retention_policy_activated: bool = False
    purge_eligible: bool = False
    closure_evaluated: bool = False
    external_provider_copies: str = "not_inspected"
    out_of_scope_copies: str = "not_inspected"

    def file_count_for(self, file_class: FileClass) -> int:
        return next(c.file_count for c in self.file_classes if c.file_class is file_class)

    def logical_bytes_for(self, file_class: FileClass) -> int:
        return next(c.logical_bytes for c in self.file_classes if c.file_class is file_class)

    def table_count_for(self, table_class: TableClass) -> int:
        return next(c.table_count for c in self.table_classes if c.table_class is table_class)

    def row_count_for(self, table_class: TableClass) -> int:
        return next(c.row_count for c in self.table_classes if c.table_class is table_class)

    def to_dict(self) -> dict[str, object]:
        """Serialise aggregates only; no input path, name, identifier or hash is included."""

        return {
            "schema_version": self.schema_version,
            "inventory_scope": self.inventory_scope,
            "file_classes": [
                {
                    "file_class": c.file_class.value,
                    "file_count": c.file_count,
                    "logical_bytes": c.logical_bytes,
                }
                for c in self.file_classes
            ],
            "table_classes": [
                {
                    "table_class": c.table_class.value,
                    "table_count": c.table_count,
                    "row_count": c.row_count,
                    "unavailable_table_count": c.unavailable_table_count,
                }
                for c in self.table_classes
            ],
            "directory_count": self.directory_count,
            "file_count": self.file_count,
            "logical_bytes": self.logical_bytes,
            "declared_database_count": self.declared_database_count,
            "inspected_database_count": self.inspected_database_count,
            "uninspected_database_count": self.uninspected_database_count,
            "unknown_table_count": self.unknown_table_count,
            "unknown_schema_object_count": self.unknown_schema_object_count,
            "row_count_unavailable_count": self.row_count_unavailable_count,
            "safety_status": self.safety_status,
            "reason_codes": list(self.reason_codes),
            "inventory_complete": self.inventory_complete,
            "retention_policy_activated": self.retention_policy_activated,
            "purge_eligible": self.purge_eligible,
            "closure_evaluated": self.closure_evaluated,
            "external_provider_copies": self.external_provider_copies,
            "out_of_scope_copies": self.out_of_scope_copies,
        }


@dataclasses.dataclass(frozen=True)
class _StatKey:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    nlink: int


@dataclasses.dataclass(frozen=True)
class _FileEntry:
    relative: str
    file_class: FileClass
    stat_key: _StatKey


@dataclasses.dataclass(frozen=True)
class _Census:
    files: dict[str, _FileEntry]
    directories: dict[str, _StatKey]
    root: _StatKey


@dataclasses.dataclass
class _TableTally:
    table_count: int = 0
    row_count: int = 0
    unavailable_table_count: int = 0


@dataclasses.dataclass
class _DatabaseTally:
    tables: dict[TableClass, _TableTally]
    unknown_schema_object_count: int = 0
    row_count_unavailable_count: int = 0


def _refuse(reason: InventoryRefused) -> InventoryRefusal:
    return InventoryRefusal(reason)


def _stat_key(result: os.stat_result) -> _StatKey:
    return _StatKey(
        dev=result.st_dev,
        ino=result.st_ino,
        size=result.st_size,
        mtime_ns=result.st_mtime_ns,
        ctime_ns=result.st_ctime_ns,
        mode=result.st_mode,
        nlink=result.st_nlink,
    )


def _validated_root(snapshot_root: Path) -> Path:
    if not isinstance(snapshot_root, Path):
        raise _refuse(InventoryRefused.INVALID_ROOT)
    if not snapshot_root.is_absolute():
        raise _refuse(InventoryRefused.ROOT_NOT_ABSOLUTE)
    text = os.fspath(snapshot_root)
    if not text or "\x00" in text:
        raise _refuse(InventoryRefused.INVALID_ROOT)
    if any(part in ("", ".", "..") for part in snapshot_root.parts[1:]):
        raise _refuse(InventoryRefused.INVALID_ROOT)
    return snapshot_root


def _validated_paths(database_paths: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(database_paths, tuple):
        raise _refuse(InventoryRefused.INVALID_DATABASE_PATH)
    seen: set[str] = set()
    for raw in database_paths:
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise _refuse(InventoryRefused.INVALID_DATABASE_PATH)
        if raw.startswith("/") or any(ch in raw for ch in "?#%:\\"):
            raise _refuse(InventoryRefused.INVALID_DATABASE_PATH)
        if any(ord(ch) < 0x20 for ch in raw):
            raise _refuse(InventoryRefused.INVALID_DATABASE_PATH)
        parts = raw.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise _refuse(InventoryRefused.INVALID_DATABASE_PATH)
        if posixpath.normpath(raw) != raw:
            raise _refuse(InventoryRefused.INVALID_DATABASE_PATH)
        if parts[-1] != _PERMITTED_DATABASE_BASENAME:
            raise _refuse(InventoryRefused.UNSUPPORTED_DATABASE_BASENAME)
        if raw in seen:
            raise _refuse(InventoryRefused.DUPLICATE_DATABASE_PATH)
        seen.add(raw)
    return tuple(database_paths)


def _is_temp_name(name: str) -> bool:
    lowered = name.lower()
    if lowered in {".ds_store", "thumbs.db"}:
        return True
    if lowered.endswith("~"):
        return True
    return any(lowered.endswith(suffix) for suffix in _TEMP_SUFFIXES)


def _is_sqlite_name(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in _SQLITE_SUFFIXES)


def _classify(relative: str, declared: frozenset[str]) -> FileClass:
    name = relative.rsplit("/", 1)[-1]
    if name.endswith("-wal"):
        return FileClass.SQLITE_WAL
    if name.endswith("-shm"):
        return FileClass.SQLITE_SHM
    if name.endswith("-journal"):
        return FileClass.SQLITE_ROLLBACK_JOURNAL
    if relative in declared:
        return FileClass.DECLARED_SQLITE_DATABASE
    parts = relative.split("/")
    if (
        len(parts) == 3
        and parts[0] == "photos"
        and _HEX64.fullmatch(parts[1]) is not None
        and _PHOTO.fullmatch(parts[2]) is not None
    ):
        return FileClass.OWNER_SCOPED_PHOTO_BLOB
    if len(parts) == 2 and parts[0] == "pdfs" and _PDF.fullmatch(parts[1]) is not None:
        return FileClass.EXPORTED_FROZEN_PDF
    if name in _CONTROL_NAMES or name.lower().endswith((".incomplete", ".active")):
        return FileClass.BACKUP_CONTROL_METADATA
    if _is_temp_name(name):
        return FileClass.TEMPORARY_COPY_CANDIDATE
    if _is_sqlite_name(name):
        return FileClass.UNINSPECTED_SQLITE_COPY_CANDIDATE
    return FileClass.PROTECTED_UNKNOWN_COPY


def _is_incomplete_marker(relative: str) -> bool:
    name = relative.rsplit("/", 1)[-1]
    lowered = name.lower()
    return (
        name in _INCOMPLETE_MARKERS
        or lowered.endswith(".incomplete")
        or lowered in {"snapshot_active", "backup_incomplete", "restore_incomplete"}
    )


def _open_root(snapshot_root: Path) -> int:
    descriptor = -1
    try:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in snapshot_root.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise _refuse(
            InventoryRefused.SYMLINK_IN_PATH if error.errno == 40 else InventoryRefused.INVALID_ROOT
        ) from None
    result = os.fstat(descriptor)
    if not stat.S_ISDIR(result.st_mode):
        os.close(descriptor)
        raise _refuse(InventoryRefused.INVALID_ROOT)
    return descriptor


def _scan(root_fd: int, root_key: _StatKey, declared: frozenset[str]) -> _Census:
    files: dict[str, _FileEntry] = {}
    directories: dict[str, _StatKey] = {}

    if _stat_key(os.fstat(root_fd)) != root_key:
        raise _refuse(InventoryRefused.CONCURRENT_MUTATION)

    def walk(directory_fd: int, prefix: str, depth: int) -> None:
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError:
            raise _refuse(InventoryRefused.READ_FAILED) from None
        for name in names:
            relative = f"{prefix}/{name}" if prefix else name
            try:
                result = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                raise _refuse(InventoryRefused.READ_FAILED) from None
            key = _stat_key(result)
            if stat.S_ISLNK(result.st_mode):
                raise _refuse(InventoryRefused.SYMLINK_IN_PATH)
            if result.st_dev != root_key.dev:
                raise _refuse(InventoryRefused.DEVICE_BOUNDARY)
            if stat.S_ISDIR(result.st_mode):
                if depth >= _MAX_DEPTH:
                    raise _refuse(InventoryRefused.DEPTH_EXCEEDED)
                try:
                    child = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                except OSError:
                    raise _refuse(InventoryRefused.READ_FAILED) from None
                try:
                    opened = _stat_key(os.fstat(child))
                    if not stat.S_ISDIR(opened.mode) or opened != key:
                        raise _refuse(InventoryRefused.CONCURRENT_MUTATION)
                    directories[relative] = key
                    walk(child, relative, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(result.st_mode):
                if key.nlink != 1:
                    raise _refuse(InventoryRefused.HARDLINKED_ENTRY)
                files[relative] = _FileEntry(
                    relative=relative,
                    file_class=_classify(relative, declared),
                    stat_key=key,
                )
            else:
                raise _refuse(InventoryRefused.UNSAFE_ENTRY)

    walk(root_fd, "", 0)
    return _Census(files=files, directories=directories, root=root_key)


def _open_pinned(root_fd: int, relative: str) -> int:
    parts = relative.split("/")
    descriptor = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        written = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=descriptor,
        )
        os.close(descriptor)
        descriptor = -1
    except OSError:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise _refuse(InventoryRefused.SYMLINK_IN_PATH) from None
    try:
        opened = os.fstat(written)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise _refuse(InventoryRefused.UNSAFE_ENTRY)
        with contextlib.suppress(OSError):
            os.set_blocking(written, True)
    except BaseException:
        os.close(written)
        raise
    return written


def _quote(name: str) -> str:
    if "\x00" in name:
        raise _refuse(InventoryRefused.IDENTIFIER_UNSAFE)
    return '"' + name.replace('"', '""') + '"'


def _table_rows(database: sqlite3.Connection) -> list[tuple[str, str]]:
    cursor = database.execute("PRAGMA table_list")
    rows: list[tuple[str, str]] = []
    try:
        while True:
            batch = cursor.fetchmany(64)
            if not batch:
                break
            for row in batch:
                schema, name, kind = row[0], row[1], row[2]
                if schema != "main" or not isinstance(name, str) or not isinstance(kind, str):
                    continue
                # sqlite_schema is the catalog pseudo-table, not ordinary persistent data.
                # Every other sqlite_* table (sqlite_sequence, sqlite_stat*) is counted.
                if name == "sqlite_schema":
                    continue
                rows.append((name, kind))
    finally:
        cursor.close()
    return rows


def _inspect_database(fd: int, expected: _StatKey) -> _DatabaseTally:
    before = os.fstat(fd)
    if _stat_key(before) != expected:
        raise _refuse(InventoryRefused.DATABASE_DESCRIPTOR_MISMATCH)
    directory = "/proc/self/fd"
    if not os.path.isdir(directory):
        raise _refuse(InventoryRefused.SQLITE_UNAVAILABLE)
    uri = f"file:{directory}/{fd}?mode=ro&immutable=1"
    try:
        database = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error:
        raise _refuse(InventoryRefused.SQLITE_UNAVAILABLE) from None
    result = _DatabaseTally(tables={})
    try:
        database.enable_load_extension(False)
        database.execute("PRAGMA trusted_schema=OFF")
        database.execute("PRAGMA query_only=ON")
        try:
            database.execute("BEGIN")
            tables = _table_rows(database)
            for name, kind in tables:
                if kind == "view":
                    result.unknown_schema_object_count += 1
                    result.row_count_unavailable_count += 1
                    continue
                if kind == "virtual":
                    result.unknown_schema_object_count += 1
                    result.row_count_unavailable_count += 1
                    continue
                table_class = (
                    TableClass.PROTECTED_UNKNOWN
                    if kind == "shadow"
                    else _KNOWN_TABLES.get(name, TableClass.PROTECTED_UNKNOWN)
                )
                entry = result.tables.setdefault(table_class, _TableTally())
                entry.table_count += 1
                try:
                    row = database.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()
                except sqlite3.OperationalError:
                    entry.unavailable_table_count += 1
                    result.row_count_unavailable_count += 1
                    continue
                if row is None or not isinstance(row[0], int):
                    entry.unavailable_table_count += 1
                    result.row_count_unavailable_count += 1
                    continue
                entry.row_count += row[0]
        finally:
            with contextlib.suppress(sqlite3.Error):
                database.execute("COMMIT")
    except sqlite3.DatabaseError:
        raise _refuse(InventoryRefused.MALFORMED_DATABASE) from None
    finally:
        database.close()
    after = os.fstat(fd)
    if _stat_key(after) != expected:
        raise _refuse(InventoryRefused.CONCURRENT_MUTATION)
    return result


def _census_equal(first: _Census, second: _Census) -> bool:
    if first.root != second.root:
        return False
    if first.directories != second.directories:
        return False
    if first.files.keys() != second.files.keys():
        return False
    return all(first.files[name].stat_key == second.files[name].stat_key for name in first.files)


def inventory_snapshot(snapshot_root: Path, *, database_paths: tuple[str, ...]) -> InventoryReport:
    """Count a supplied standalone snapshot tree without mutating or deleting anything."""

    root = _validated_root(snapshot_root)
    paths = _validated_paths(database_paths)
    declared = frozenset(paths)
    root_fd = _open_root(root)
    try:
        root_key = _stat_key(os.fstat(root_fd))
        before = _scan(root_fd, root_key, declared)
        for relative in paths:
            entry = before.files.get(relative)
            if entry is None or entry.file_class is not FileClass.DECLARED_SQLITE_DATABASE:
                raise _refuse(InventoryRefused.MISSING_DATABASE)

        marker = any(_is_incomplete_marker(name) for name in before.files)
        blocked = marker
        unavailable = 0
        schema_objects = 0
        tallies: dict[TableClass, _TableTally] = {}
        inspected = 0
        for relative in paths:
            sidecars = {
                relative + "-wal",
                relative + "-shm",
                relative + "-journal",
            }
            entry = before.files[relative]
            if marker or sidecars & before.files.keys():
                blocked = True
                continue
            fd = _open_pinned(root_fd, relative)
            try:
                current = _stat_key(os.fstat(fd))
                if current != entry.stat_key:
                    raise _refuse(InventoryRefused.DATABASE_DESCRIPTOR_MISMATCH)
                counted = _inspect_database(fd, entry.stat_key)
            finally:
                os.close(fd)
            inspected += 1
            schema_objects += counted.unknown_schema_object_count
            unavailable += counted.row_count_unavailable_count
            for table_class, tally in counted.tables.items():
                target = tallies.setdefault(table_class, _TableTally())
                target.table_count += tally.table_count
                target.row_count += tally.row_count
                target.unavailable_table_count += tally.unavailable_table_count

        after = _scan(root_fd, root_key, declared)
        if not _census_equal(before, after):
            raise _refuse(InventoryRefused.CONCURRENT_MUTATION)
        try:
            root_after = _stat_key(os.stat(root, follow_symlinks=False))
        except OSError:
            raise _refuse(InventoryRefused.CONCURRENT_MUTATION) from None
        if root_after != root_key:
            raise _refuse(InventoryRefused.CONCURRENT_MUTATION)
    finally:
        os.close(root_fd)

    file_counts: dict[FileClass, tuple[int, int]] = {}
    for entry in before.files.values():
        count, size = file_counts.get(entry.file_class, (0, 0))
        file_counts[entry.file_class] = (count + 1, size + entry.stat_key.size)

    file_classes = tuple(
        FileClassCount(
            file_class=file_class,
            file_count=file_counts.get(file_class, (0, 0))[0],
            logical_bytes=file_counts.get(file_class, (0, 0))[1],
        )
        for file_class in FileClass
    )
    table_classes = tuple(
        TableClassCount(
            table_class=table_class,
            table_count=tallies.get(table_class, _TableTally()).table_count,
            row_count=tallies.get(table_class, _TableTally()).row_count,
            unavailable_table_count=tallies.get(table_class, _TableTally()).unavailable_table_count,
        )
        for table_class in TableClass
    )
    unknown_tables = tallies.get(TableClass.PROTECTED_UNKNOWN, _TableTally()).table_count
    declared_count = len(paths)
    uninspected = declared_count - inspected
    reason_codes: list[str] = []
    if uninspected:
        reason_codes.append("database_inspection_blocked")
    if unavailable:
        reason_codes.append("row_count_unavailable")
    if schema_objects:
        reason_codes.append("unknown_schema_objects_present")
    if unknown_tables:
        reason_codes.append("unknown_tables_present")
    blocked = blocked or bool(uninspected) or bool(unavailable) or bool(schema_objects)
    safety_status = "blocked" if blocked else "clear"
    return InventoryReport(
        schema_version=SCHEMA_VERSION,
        inventory_scope=INVENTORY_SCOPE,
        file_classes=file_classes,
        table_classes=table_classes,
        directory_count=len(before.directories),
        file_count=len(before.files),
        logical_bytes=sum(entry.stat_key.size for entry in before.files.values()),
        declared_database_count=declared_count,
        inspected_database_count=inspected,
        uninspected_database_count=uninspected,
        unknown_table_count=unknown_tables,
        unknown_schema_object_count=schema_objects,
        row_count_unavailable_count=unavailable,
        safety_status=safety_status,
        reason_codes=tuple(reason_codes),
        inventory_complete=not blocked,
    )


__all__ = [
    "FileClass",
    "FileClassCount",
    "InventoryRefusal",
    "InventoryRefused",
    "InventoryReport",
    "TableClass",
    "TableClassCount",
    "inventory_snapshot",
]
