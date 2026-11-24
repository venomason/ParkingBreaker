"""Local evidence for route revision history, concurrency and frozen attempt lineage."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mail_samples import make_request
from parkingbreaker.mail_attempt_store import SqliteMailAttemptStore, StorageCorruption
from parkingbreaker.mail_config import MailClass
from parkingbreaker.route_registry import RouteConflict, RouteScope
from parkingbreaker.transaction_store import RevisionConflict, SqliteTransactionStore

NOW = datetime(2026, 11, 23, tzinfo=UTC)
EXPIRES = NOW + timedelta(days=2)
SCOPE = RouteScope("CA:example.gov", "first-level review", MailClass.CERTIFIED)
ADDRESS = "a" * 64


def _verification(**overrides: Any) -> dict[str, Any]:
    return {
        "scope": SCOPE,
        "expected_revision": 0,
        "command_id": "route-enrollment",
        "status": "verified",
        "address_fingerprint": ADDRESS,
        "source_url": "https://parking.example.gov/initial-review",
        "source_quote": "Mail initial review requests to Parking Appeals, PO Box 1.",
        "evidence_sha256": "b" * 64,
        "source_text_sha256": "c" * 64,
        "valid_until": EXPIRES,
        "now": NOW,
    } | overrides


@pytest.mark.parametrize(
    "alias",
    [
        "initial review",
        "Administrative Review",
        " FIRST\u2011LEVEL   REVIEW ",
        "first level review",
        "first-stage review",
        "First Stage Review",
        "initial protest",
        "First-Level Protest",
        "first stage protest",
    ],
)
def test_clear_first_stage_aliases_resolve_to_the_same_scope(alias: str) -> None:
    assert replace(SCOPE, stage=alias) == SCOPE


def test_hearing_request_and_second_level_review_share_a_distinct_scope() -> None:
    hearing = replace(SCOPE, stage="administrative hearing")
    assert replace(SCOPE, stage="hearing request") == replace(SCOPE, stage="second-level review")
    assert replace(SCOPE, stage="hearing request") == hearing
    assert hearing != SCOPE


def test_route_history_replays_after_restart_and_preserves_scope(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        first = store.change_route(**_verification())
        assert first.verified_at(NOW)
        assert not first.verified_at(NOW - timedelta(seconds=1))
        assert not first.verified_at(EXPIRES)
        assert first.source_text_sha256 == "c" * 64
        for other in (
            replace(SCOPE, issuer="CA:another.gov"),
            replace(SCOPE, stage="administrative hearing"),
            replace(SCOPE, stage="appeal"),
            replace(SCOPE, mail_class=MailClass.FIRST_CLASS),
        ):
            assert store.current_route(other) is None
        revoked = store.change_route(
            **_verification(
                command_id="revoke",
                status="revoked",
                expected_revision=1,
                now=NOW + timedelta(minutes=1),
            )
        )
    with SqliteTransactionStore(path) as store:
        assert store.monitored_routes() == (revoked,)
        assert store.current_route(replace(SCOPE, stage="first-stage review")) == revoked
        assert store.change_route(**_verification(now=EXPIRES)) == first
        assert not revoked.verified_at(NOW + timedelta(minutes=2))
        with pytest.raises(RouteConflict, match="already in use"):
            store.change_route(**_verification(address_fingerprint="d" * 64))
        assert store.current_route(SCOPE) == revoked


def test_stale_receipt_prevents_delayed_application_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    future = _verification(expected_revision=1, command_id="premature")
    with SqliteTransactionStore(path) as store:
        with pytest.raises(RouteConflict, match="expected 1, saw 0"):
            store.change_route(**future)
        assert store.current_route(SCOPE) is None
        store.change_route(**_verification())
    with SqliteTransactionStore(path) as store:
        with pytest.raises(RouteConflict, match="expected 1, saw 0"):
            store.change_route(**future)
        next_revision = store.change_route(
            **_verification(expected_revision=1, command_id="new-command")
        )
        assert next_revision.revision == 2
        assert store.current_route(SCOPE) == next_revision


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("address_fingerprint", "bad-digest"),
        ("source_url", None),
        ("source_url", "http://parking.example.gov/initial-review"),
        ("source_url", "https://user@parking.example.gov/initial-review"),
        ("source_url", "https://127.0.0.1/initial-review"),
        ("source_url", "https://parking.example.gov:8080/initial-review"),
        ("source_quote", "  "),
        ("evidence_sha256", None),
        ("source_text_sha256", "bad-digest"),
        ("valid_until", None),
        ("valid_until", NOW.replace(tzinfo=None)),
        ("valid_until", NOW),
        ("now", NOW.replace(tzinfo=None)),
    ],
)
def test_verified_command_requires_safe_evidence_and_expiry(
    tmp_path: Path, field: str, invalid: object
) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        with pytest.raises(ValueError, match=r".+"):
            store.change_route(**_verification(**{field: invalid}))
        assert store.monitored_routes() == ()


def test_route_change_shares_mail_create_lock_and_busy_id_can_retry(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as create, SqliteTransactionStore(path) as operator:
        with create.submission_ownership(), pytest.raises(RevisionConflict, match="coordinator"):
            operator.change_route(**_verification())
        assert operator.current_route(SCOPE) is None
        assert operator.change_route(**_verification()).revision == 1


def test_failed_revision_write_rolls_back_command(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        database = store._require_open()
        database.execute(
            "CREATE TEMP TRIGGER reject_route BEFORE INSERT ON pb_municipal_route_revisions "
            "BEGIN SELECT RAISE(ABORT, 'synthetic insert failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="synthetic insert failure"):
            store.change_route(**_verification())
        assert database.execute("SELECT * FROM pb_municipal_route_commands").fetchall() == []
        assert store.current_route(SCOPE) is None
        database.execute("DROP TRIGGER temp.reject_route")
        assert store.change_route(**_verification()).revision == 1


def test_withdrawn_address_follows_revisions_and_hard_expiry(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        unknown = "d" * 64
        assert not store.withdrawn_address(ADDRESS, NOW)
        store.change_route(**_verification())
        assert not store.withdrawn_address(ADDRESS, NOW)
        assert store.withdrawn_address(ADDRESS, EXPIRES)
        assert not store.withdrawn_address(unknown, EXPIRES)
        store.change_route(
            **_verification(expected_revision=1, command_id="suspect", status="suspended")
        )
        assert store.withdrawn_address(ADDRESS, NOW)
        store.change_route(**_verification(expected_revision=2, command_id="recheck"))
        assert not store.withdrawn_address(ADDRESS, NOW)
        store.change_route(
            **_verification(expected_revision=3, command_id="replace", address_fingerprint=unknown)
        )
        assert store.withdrawn_address(ADDRESS, NOW)
        assert not store.withdrawn_address(unknown, NOW)


def test_migrate_without_binding_existing_mail_and_keep_per_attempt_revision(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteMailAttemptStore(path) as legacy:
        prepared = legacy.prepare("legacy", 1, make_request(token="legacy-token"), NOW)
    with SqliteTransactionStore(path) as store:
        assert store.get("legacy", 1) == prepared
        assert store.bound_route("legacy", 1) is None
        with pytest.raises(StorageCorruption, match="mail freeze transaction"):
            store.bind_route("legacy", 1, SCOPE, 0)

        database = store._require_open()
        database.execute("BEGIN IMMEDIATE")
        store.prepare("unmanaged", 1, make_request(token="unmanaged-token"), NOW)
        store.bind_route("unmanaged", 1, SCOPE, 0)
        database.execute("COMMIT")

        store.change_route(**_verification())
        database.execute("BEGIN IMMEDIATE")
        store.prepare("monitored", 1, make_request(token="monitored-first"), NOW)
        with pytest.raises(RouteConflict, match="before the mail attempt froze"):
            store.bind_route("monitored", 1, SCOPE, 0)
        store.bind_route("monitored", 1, SCOPE, 1)
        database.execute("COMMIT")

        store.change_route(**_verification(expected_revision=1, command_id="renewal"))
        database.execute("BEGIN IMMEDIATE")
        store.bind_route("monitored", 1, SCOPE, 1)
        with pytest.raises(RouteConflict, match="bound to a different route revision"):
            store.bind_route("monitored", 1, SCOPE, 2)
        store.prepare("monitored", 2, make_request(token="monitored-second"), NOW)
        store.bind_route("monitored", 2, SCOPE, 2)
        database.execute("COMMIT")

    with SqliteTransactionStore(path) as store:
        assert store.bound_route("legacy", 1) is None
        assert store.bound_route("unmanaged", 1) == (SCOPE, 0)
        assert store.bound_route("monitored", 1) == (SCOPE, 1)
        assert store.bound_route("monitored", 2) == (SCOPE, 2)
        assert store._require_open().execute("PRAGMA foreign_key_check").fetchall() == []


def test_freeze_rollback_leaves_no_route_binding_or_mail_attempt(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        database = store._require_open()
        database.execute("BEGIN IMMEDIATE")
        store.prepare("case-1", 1, make_request(), NOW)
        store.bind_route("case-1", 1, SCOPE, 0)
        database.execute("ROLLBACK")
        assert store.get("case-1", 1) is None
        assert store.bound_route("case-1", 1) is None


@pytest.mark.parametrize(
    ("statement", "error"),
    [
        ("DELETE FROM pb_municipal_route_commands", "history is immutable"),
        ("UPDATE pb_municipal_route_revisions SET status='suspended'", "history is immutable"),
        (
            "REPLACE INTO pb_municipal_route_revisions SELECT * FROM pb_municipal_route_revisions",
            "history must append",
        ),
        (
            "UPDATE pb_mail_route_bindings SET route_revision=NULL",
            "history is immutable",
        ),
    ],
)
def test_route_rows_are_append_only(tmp_path: Path, statement: str, error: str) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        first = store.change_route(**_verification())
        database = store._require_open()
        database.execute("BEGIN IMMEDIATE")
        store.prepare("case-1", 1, make_request(), NOW)
        store.bind_route("case-1", 1, SCOPE, 1)
        database.execute("COMMIT")
        with pytest.raises(sqlite3.IntegrityError, match=error):
            database.execute(statement)
        assert store.current_route(SCOPE) == first
        assert store.bound_route("case-1", 1) == (SCOPE, 1)


def test_missing_history_protection_fails_on_open(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        store._require_open().execute("DROP TRIGGER pb_municipal_route_revisions_no_delete")
    with pytest.raises(StorageCorruption, match="malformed municipal route schema"):
        SqliteTransactionStore(path)
