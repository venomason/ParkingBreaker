from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.transaction_store import EffectConflict, SqliteTransactionStore
from parkingbreaker.workspace import Workspace

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
V3_OUTBOX_DDL = """CREATE TABLE pb_pending_effects (
    effect_id TEXT PRIMARY KEY, case_token TEXT,
    revision INTEGER, machine TEXT NOT NULL,
    effect TEXT NOT NULL, created_at TEXT NOT NULL,
    claimed_at TEXT, outcome TEXT, completed_at TEXT,
    request_token TEXT, counter_json TEXT,
    FOREIGN KEY (case_token, revision, machine)
        REFERENCES pb_case_history(case_token, revision, machine),
    UNIQUE (request_token, effect),
    CHECK ((outcome IS NULL) = (completed_at IS NULL)),
    CHECK (completed_at IS NULL OR claimed_at IS NOT NULL),
    CHECK ((machine = 'counter' AND request_token IS NOT NULL AND counter_json IS NOT NULL
            AND case_token IS NULL AND revision IS NULL)
        OR (machine IN ('letter', 'payment') AND request_token IS NULL AND counter_json IS NULL
            AND case_token IS NOT NULL AND revision IS NOT NULL))
)"""


def _initialized_db(path: Path) -> str:
    with SqliteTransactionStore(path) as store:
        store.save_workspace("workspace", Workspace(), NOW)
        case = store.start_case("workspace", "authorization", NOW, NOW + timedelta(days=5))
    return case.case_token


V4_CASES_DDL = """CREATE TABLE pb_cases (
    case_token TEXT PRIMARY KEY,
    workspace_token TEXT NOT NULL REFERENCES pb_workspaces(workspace_token),
    authorization_ref TEXT NOT NULL UNIQUE, authorized_at TEXT NOT NULL,
    authorization_expires_at TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    letter_state TEXT NOT NULL, payment_state TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt IN (1, 2)),
    revision INTEGER NOT NULL CHECK (revision >= 1)
)"""


def _downgrade_outbox_to_v3(
    database: sqlite3.Connection, case_token: str, *, seed_rows: bool
) -> None:
    # v3 predates the no-charge CHECK and amount_cents >= 0, so rebuild pb_cases to its
    # actual historical paid schema too. Renaming the parent under legacy ALTER semantics
    # with enforcement off keeps pb_case_history's foreign key on the stable table name;
    # both pragmas are set before any DML opens a transaction and restored afterwards.
    database.commit()
    database.execute("PRAGMA foreign_keys=OFF")
    database.execute("PRAGMA legacy_alter_table=ON")
    try:
        database.execute("DROP TABLE pb_pending_effects")
        database.execute(V3_OUTBOX_DDL)
        database.execute("ALTER TABLE pb_cases RENAME TO pb_cases_v5")
        database.execute(V4_CASES_DDL)
        database.execute("INSERT INTO pb_cases SELECT * FROM pb_cases_v5")
        database.execute("DROP TABLE pb_cases_v5")
        database.execute("UPDATE pb_store_versions SET version=3 WHERE component='transactions'")
        if seed_rows:
            for revision, machine in ((2, "letter"), (3, "payment"), (4, "letter")):
                database.execute(
                    "INSERT INTO pb_case_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        case_token,
                        revision,
                        machine,
                        "source-bytes-" + str(revision),
                        "target-bytes-" + str(revision),
                        "event-" + str(revision),
                        '{"exact":"event-' + str(revision) + '"}',
                        f"2026-10-17T12:0{revision}:00+00:00",
                    ),
                )
            case_rows = (
                (
                    "letter-pending",
                    case_token,
                    2,
                    "letter",
                    "LOOKUP_REQUIREMENTS",
                    NOW.isoformat(),
                    None,
                    None,
                    None,
                ),
                (
                    "payment-claimed",
                    case_token,
                    3,
                    "payment",
                    "CAPTURE",
                    NOW.isoformat(),
                    (NOW + timedelta(minutes=1)).isoformat(),
                    None,
                    None,
                ),
                (
                    "letter-done",
                    case_token,
                    4,
                    "letter",
                    "SUBMIT_LETTER",
                    NOW.isoformat(),
                    (NOW + timedelta(minutes=2)).isoformat(),
                    "submitted",
                    (NOW + timedelta(minutes=3)).isoformat(),
                ),
            )
            database.executemany(
                "INSERT INTO pb_pending_effects "
                "(effect_id, case_token, revision, machine, effect, created_at, claimed_at, "
                "outcome, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                case_rows,
            )
            counter_rows = (
                (
                    "counter-pending",
                    "REQUEST_RECEIVED",
                    NOW.isoformat(),
                    None,
                    None,
                    None,
                    "req-a",
                    '{"milestone":"REQUEST_RECEIVED"}',
                ),
                (
                    "counter-claimed",
                    "RESEARCH_STARTED",
                    NOW.isoformat(),
                    (NOW + timedelta(minutes=1)).isoformat(),
                    None,
                    None,
                    "req-b",
                    '{"milestone":"RESEARCH_STARTED"}',
                ),
                (
                    "counter-done",
                    "APPEAL_COMPLETED",
                    NOW.isoformat(),
                    (NOW + timedelta(minutes=2)).isoformat(),
                    "emitted",
                    (NOW + timedelta(minutes=3)).isoformat(),
                    "req-c",
                    '{"milestone":"APPEAL_COMPLETED"}',
                ),
            )
            database.executemany(
                "INSERT INTO pb_pending_effects "
                "(effect_id, machine, effect, created_at, claimed_at, outcome, completed_at, "
                "request_token, counter_json) "
                "VALUES (?, 'counter', ?, ?, ?, ?, ?, ?, ?)",
                counter_rows,
            )
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []
        assert database.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        database.commit()
    finally:
        if database.in_transaction:
            database.rollback()
        database.execute("PRAGMA legacy_alter_table=OFF")
        database.execute("PRAGMA foreign_keys=ON")


def test_v3_migration_preserves_every_outbox_row_and_history(tmp_path: Path) -> None:
    path = tmp_path / "transactions.sqlite"
    case_token = _initialized_db(path)
    with sqlite3.connect(path) as database:
        _downgrade_outbox_to_v3(database, case_token, seed_rows=True)
        before_rows = tuple(
            database.execute("SELECT * FROM pb_pending_effects ORDER BY effect_id").fetchall()
        )
        before_history = tuple(
            database.execute(
                "SELECT * FROM pb_case_history WHERE case_token=? ORDER BY revision", (case_token,)
            ).fetchall()
        )

    with SqliteTransactionStore(path) as store:
        assert store.get_case(case_token) is not None
    with sqlite3.connect(path) as database:
        assert database.execute(
            "SELECT version FROM pb_store_versions WHERE component='transactions'"
        ).fetchone() == (5,)
        assert (
            tuple(
                database.execute("SELECT * FROM pb_pending_effects ORDER BY effect_id").fetchall()
            )
            == before_rows
        )
        assert (
            tuple(
                database.execute(
                    "SELECT * FROM pb_case_history WHERE case_token=? ORDER BY revision",
                    (case_token,),
                ).fetchall()
            )
            == before_history
        )
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []
        assert database.execute(
            "SELECT machine, claimed_at, outcome, completed_at FROM pb_pending_effects "
            "WHERE effect_id='counter-pending'"
        ).fetchone() == ("counter", None, None, None)


def test_fresh_case_free_notification_row_and_unique_request_effect(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite"
    with SqliteTransactionStore(path):
        pass
    with sqlite3.connect(path) as database:
        assert database.execute(
            "SELECT version FROM pb_store_versions WHERE component='transactions'"
        ).fetchone() == (5,)
        row = (
            "notification-one",
            "notification",
            "SEND_SIGN_IN_LINK",
            NOW.isoformat(),
            "link-request-1",
        )
        database.execute(
            "INSERT INTO pb_pending_effects "
            "(effect_id, machine, effect, created_at, request_token) VALUES (?, ?, ?, ?, ?)",
            row,
        )
        with pytest.raises(sqlite3.IntegrityError):
            database.execute(
                "INSERT INTO pb_pending_effects "
                "(effect_id, machine, effect, created_at, request_token) VALUES (?, ?, ?, ?, ?)",
                (
                    "notification-two",
                    "notification",
                    "SEND_SIGN_IN_LINK",
                    NOW.isoformat(),
                    "link-request-1",
                ),
            )
        assert database.execute(
            "SELECT case_token, revision, counter_json FROM pb_pending_effects WHERE effect_id=?",
            ("notification-one",),
        ).fetchone() == (None, None, None)


@pytest.mark.parametrize(
    "values",
    [
        ("bad-machine", "OTHER", "request"),
        ("bad-effect", "SEND_OTHER", "request"),
        ("has-case", "SEND_SIGN_IN_LINK", "request"),
        ("has-counter", "SEND_SIGN_IN_LINK", "request"),
        ("missing-request", "SEND_SIGN_IN_LINK", None),
    ],
)
def test_notification_branch_rejects_malformed_rows(
    tmp_path: Path, values: tuple[str, str, str | None]
) -> None:
    path = tmp_path / "invalid.sqlite"
    with SqliteTransactionStore(path):
        pass
    effect_id, effect, request_token = values
    with sqlite3.connect(path) as database:
        case_token: str | None = None
        revision: int | None = None
        counter_json: str | None = None
        if effect_id == "has-case":
            case_token = "invented-case"
            revision = 2
        if effect_id == "has-counter":
            counter_json = '{"milestone":"REQUEST_RECEIVED"}'
        with pytest.raises(sqlite3.IntegrityError):
            database.execute(
                "INSERT INTO pb_pending_effects "
                "(effect_id, case_token, revision, machine, effect, created_at, "
                "request_token, counter_json) "
                "VALUES (?, ?, ?, 'notification', ?, ?, ?, ?)",
                (
                    effect_id,
                    case_token,
                    revision,
                    effect,
                    NOW.isoformat(),
                    request_token,
                    counter_json,
                ),
            )


def test_generic_effect_claim_cannot_claim_notification(tmp_path: Path) -> None:
    path = tmp_path / "notification.sqlite"
    with SqliteTransactionStore(path) as store:
        store._require_open().execute(
            "INSERT INTO pb_pending_effects "
            "(effect_id, machine, effect, created_at, request_token) "
            "VALUES (?, 'notification', 'SEND_SIGN_IN_LINK', ?, ?)",
            ("notification-effect", NOW.isoformat(), "link-request"),
        )
        with pytest.raises(EffectConflict, match="does not exist"):
            store.claim_effect("notification-effect", NOW + timedelta(minutes=1))
        assert store._require_open().execute(
            "SELECT claimed_at FROM pb_pending_effects WHERE effect_id='notification-effect'"
        ).fetchone() == (None,)


def test_v3_schema_drift_fails_without_partial_migration(tmp_path: Path) -> None:
    path = tmp_path / "drift.sqlite"
    case_token = _initialized_db(path)
    with sqlite3.connect(path) as database:
        _downgrade_outbox_to_v3(database, case_token, seed_rows=True)
        database.execute("ALTER TABLE pb_pending_effects ADD COLUMN unexpected TEXT")
        before_rows = tuple(database.execute("SELECT * FROM pb_pending_effects").fetchall())
    with pytest.raises(StorageCorruption, match="invalid request outbox schema"):
        SqliteTransactionStore(path)
    with sqlite3.connect(path) as database:
        assert database.execute(
            "SELECT version FROM pb_store_versions WHERE component='transactions'"
        ).fetchone() == (3,)
        assert tuple(database.execute("SELECT * FROM pb_pending_effects").fetchall()) == before_rows
        assert (
            database.execute(
                "SELECT 1 FROM sqlite_master WHERE name='pb_pending_effects_v3'"
            ).fetchone()
            is None
        )


def test_v3_migration_failure_rolls_back_schema_copy_and_version(tmp_path: Path) -> None:
    path = tmp_path / "rollback.sqlite"
    case_token = _initialized_db(path)
    with sqlite3.connect(path) as database:
        _downgrade_outbox_to_v3(database, case_token, seed_rows=True)
        before_rows = tuple(
            database.execute("SELECT * FROM pb_pending_effects ORDER BY effect_id").fetchall()
        )
        database.execute(
            "CREATE TRIGGER reject_version_four BEFORE UPDATE OF version ON pb_store_versions "
            "WHEN NEW.component='transactions' AND NEW.version=4 "
            "BEGIN SELECT RAISE(ABORT, 'synthetic migration failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic migration failure"):
        SqliteTransactionStore(path)
    with sqlite3.connect(path) as database:
        assert database.execute(
            "SELECT version FROM pb_store_versions WHERE component='transactions'"
        ).fetchone() == (3,)
        assert (
            tuple(
                database.execute("SELECT * FROM pb_pending_effects ORDER BY effect_id").fetchall()
            )
            == before_rows
        )
        assert (
            database.execute(
                "SELECT 1 FROM sqlite_master WHERE name='pb_pending_effects_v3'"
            ).fetchone()
            is None
        )


def test_unknown_version_and_unversioned_outbox_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "unknown.sqlite"
    with SqliteTransactionStore(path):
        pass
    with sqlite3.connect(path) as database:
        database.execute("UPDATE pb_store_versions SET version=6 WHERE component='transactions'")
    with pytest.raises(StorageCorruption, match="unsupported transactions schema version"):
        SqliteTransactionStore(path)

    unversioned = tmp_path / "unversioned.sqlite"
    _initialized_db(unversioned)
    with sqlite3.connect(unversioned) as database:
        database.execute("DELETE FROM pb_store_versions WHERE component='transactions'")
    with pytest.raises(StorageCorruption, match="without a known version"):
        SqliteTransactionStore(unversioned)

    orphan = tmp_path / "orphan-outbox.sqlite"
    with sqlite3.connect(orphan) as database:
        database.execute("CREATE TABLE pb_pending_effects (effect_id TEXT PRIMARY KEY)")
    with pytest.raises(StorageCorruption, match="without a known version"):
        SqliteTransactionStore(orphan)
    with sqlite3.connect(orphan) as database:
        assert (
            database.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pb_workspaces'"
            ).fetchone()
            is None
        )
