"""Tests for the outcome follow-up record: consent, one send per window, honest outcomes."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from parkingbreaker.outcome_follow_up_store import (
    _DDL_V1,
    FollowUpConflict,
    canonical_email_key,
)
from parkingbreaker.transaction_store import SqliteTransactionStore

NOW = datetime(2026, 11, 21, 12, tzinfo=UTC)
EMAIL = "driver@example.test"
CASE = "case_token_restart"


def _store(path: Path) -> SqliteTransactionStore:
    return SqliteTransactionStore(path)


def test_consent_defaults_allowed_and_opt_out_is_honored_until_opt_in(tmp_path: Path) -> None:
    with _store(tmp_path / "consent.sqlite") as store:
        assert store.follow_up_allowed(EMAIL) is True
        out = store.set_follow_up_consent(
            EMAIL, False, "out-1", "customer", "customer opt-out", now=NOW
        )
        assert out.kind == "OPT_OUT"
        assert store.follow_up_allowed(EMAIL) is False
        back = store.set_follow_up_consent(
            EMAIL, True, "in-1", "customer", "customer opt-in", now=NOW
        )
        assert back.kind == "OPT_IN"
        assert store.follow_up_allowed(EMAIL) is True


def test_consent_key_is_hashed_and_never_stores_the_address(tmp_path: Path) -> None:
    path = tmp_path / "hashed.sqlite"
    with _store(path) as store:
        store.set_follow_up_consent(EMAIL, False, "out-h", "customer", "opt-out", now=NOW)
        assert canonical_email_key("  Driver@Example.TEST ") == canonical_email_key(EMAIL)
    raw = path.read_bytes()
    assert EMAIL.encode() not in raw
    # Only the hex consent key is stored; the address itself never appears.
    assert canonical_email_key(EMAIL).encode() in raw


def test_one_send_per_window_and_windows_are_independent(tmp_path: Path) -> None:
    path = tmp_path / "sends.sqlite"
    with _store(path) as store:
        assert store.follow_up_sent(CASE, "D14") is False
        store.record_follow_up_send(CASE, "D14", "send-d14", "worker", now=NOW)
        assert store.follow_up_sent(CASE, "D14") is True
        with pytest.raises(FollowUpConflict, match="follow_up_already_recorded"):
            store.record_follow_up_send(CASE, "D14", "send-d14-again", "worker", now=NOW)
        assert store.follow_up_sent(CASE, "D30") is False
        store.record_follow_up_send(CASE, "D30", "send-d30", "worker", now=NOW)
        with pytest.raises(FollowUpConflict):
            store.record_follow_up_send(CASE, "D14x", "send-bad", "worker", now=NOW)
    with _store(path) as store:
        assert store.follow_up_sent(CASE, "D14") is True
        assert store.follow_up_sent(CASE, "D30") is True


def test_outcomes_keep_agency_and_customer_distinct_and_unknown_is_the_fallback(
    tmp_path: Path,
) -> None:
    with _store(tmp_path / "outcomes.sqlite") as store:
        assert store.outcome_for(CASE) is None
        agency = store.record_outcome(
            CASE, "OUTCOME_AGENCY", "agency notice: dismissed", "agency-1", "operator", now=NOW
        )
        assert agency.kind == "OUTCOME_AGENCY"
        found = store.outcome_for(CASE)
        assert found is not None
        assert found.kind == "OUTCOME_AGENCY"
        with pytest.raises(FollowUpConflict):
            store.record_outcome(
                CASE, "OUTCOME_CUSTOMER", "customer says dismissed", "cust-1", "operator", now=NOW
            )
        with pytest.raises(FollowUpConflict):
            store.record_unknown_outcome(CASE, "unknown-1", "worker", now=NOW)
        with pytest.raises(FollowUpConflict, match="agency or customer"):
            store.record_outcome(CASE, "OUTCOME_UNKNOWN", "", "bad", "operator", now=NOW)


def test_unknown_is_recorded_once_when_nothing_was_reported(tmp_path: Path) -> None:
    with _store(tmp_path / "unknown.sqlite") as store:
        store.record_follow_up_send(CASE, "D14", "s14", "worker", now=NOW)
        store.record_follow_up_send(CASE, "D30", "s30", "worker", now=NOW)
        unknown = store.record_unknown_outcome(CASE, "unknown-1", "worker", now=NOW)
        assert unknown.kind == "OUTCOME_UNKNOWN"
        assert store.outcome_for(CASE) == unknown
        with pytest.raises(FollowUpConflict):
            store.record_unknown_outcome(CASE, "unknown-2", "worker", now=NOW)


def test_command_replay_is_idempotent_and_a_changed_payload_refuses(tmp_path: Path) -> None:
    with _store(tmp_path / "replay.sqlite") as store:
        saved = store.record_follow_up_send(CASE, "D14", "cmd-1", "worker", "due", now=NOW)
        replay = store.record_follow_up_send(CASE, "D14", "cmd-1", "worker", "due", now=NOW)
        assert replay.replayed is True
        assert replay.revision == saved.revision
        with pytest.raises(FollowUpConflict, match="command_id_payload_changed"):
            store.record_follow_up_send(CASE, "D30", "cmd-1", "worker", "due", now=NOW)


def test_history_is_append_only(tmp_path: Path) -> None:
    path = tmp_path / "append.sqlite"
    with _store(path) as store:
        store.record_follow_up_send(CASE, "D14", "send-1", "worker", now=NOW)
        database = store._require_open()
        for statement in (
            "UPDATE pb_outcome_follow_up_events SET detail='changed'",
            "DELETE FROM pb_outcome_follow_up_events",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                database.execute(statement)


def test_schema_rejects_a_case_less_outcome_and_an_email_less_consent(tmp_path: Path) -> None:
    with _store(tmp_path / "schema.sqlite") as store:
        database = store._require_open()
        with pytest.raises(sqlite3.IntegrityError):
            database.execute(
                "INSERT INTO pb_outcome_follow_up_events VALUES "
                "(99, NULL, NULL, 'OUTCOME_AGENCY', '', 'bad-1', 'operator', 'r', "
                "'2026-11-21T12:00:00+00:00')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            database.execute(
                "INSERT INTO pb_outcome_follow_up_events VALUES "
                "(100, 'case', NULL, 'OPT_OUT', '', 'bad-2', 'operator', 'r', "
                "'2026-11-21T12:00:00+00:00')"
            )


def test_v1_migration_preserves_consent_reminders_and_outcome_uniqueness(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    with _store(path) as store:
        store.set_follow_up_consent(EMAIL, False, "opt-out", "customer", now=NOW)
        store.record_follow_up_send(CASE, "D14", "sent", "worker", now=NOW)
        store.record_unknown_outcome(CASE, "unknown", "worker", now=NOW)
        recorded = store._follow_up_events()
        db = store._require_open()
        old_rows = db.execute("SELECT * FROM pb_outcome_follow_up_events").fetchall()
        db.execute("DROP TABLE pb_outcome_follow_up_events")
        db.execute(_DDL_V1)
        db.executemany(
            "INSERT INTO pb_outcome_follow_up_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", old_rows
        )
        db.execute("UPDATE pb_store_versions SET version=1 WHERE component='outcome_follow_up'")
    with _store(path) as upgraded:
        assert upgraded._follow_up_events() == recorded
        assert not upgraded.follow_up_allowed(EMAIL)
        assert upgraded.follow_up_sent(CASE, "D14")
        with pytest.raises(FollowUpConflict, match="follow_up_already_recorded"):
            upgraded.record_unknown_outcome(CASE, "duplicate-unknown", "worker", now=NOW)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            upgraded._require_open().execute("DELETE FROM pb_outcome_follow_up_events")
