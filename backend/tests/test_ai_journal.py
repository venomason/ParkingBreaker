from __future__ import annotations

import ast
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import parkingbreaker
from parkingbreaker.oplog.ai_journal import AiCallStatus, AiJournalEntry, AiNote, AiWorker
from parkingbreaker.transaction_store import SqliteTransactionStore

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
ENTRY = AiJournalEntry(AiWorker.RESEARCH, AiNote.DEADLINE_UNCLEAR, AiCallStatus.OK, 2, 900, 120)


def test_entries_admit_only_categories_and_bounded_counts() -> None:
    with pytest.raises(ValueError, match="worker"):
        AiJournalEntry("RESEARCH", AiNote.SOURCE_FOUND, AiCallStatus.OK)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="bounded count"):
        AiJournalEntry(AiWorker.RESEARCH, AiNote.SOURCE_FOUND, AiCallStatus.OK, calls=-1)
    with pytest.raises(ValueError, match="bounded count"):
        AiJournalEntry(AiWorker.RESEARCH, AiNote.SOURCE_FOUND, AiCallStatus.OK, calls=True)
    assert AiJournalEntry.from_json(ENTRY.to_json()) == ENTRY
    smuggled = json.loads(ENTRY.to_json()) | {"note_text": "Customer said plate 7ABC123"}
    with pytest.raises(ValueError, match="unknown or missing"):
        AiJournalEntry.from_json(json.dumps(smuggled))
    with pytest.raises(ValueError, match="Free text"):
        AiJournalEntry.from_json(ENTRY.to_json().replace("DEADLINE_UNCLEAR", "Free text"))


def test_journal_is_append_only_and_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite"
    fallback = AiJournalEntry(
        AiWorker.PACKET_CHECK, AiNote.NOTHING_NOTABLE, AiCallStatus.FALLBACK_AFTER_FAILURE
    )
    with SqliteTransactionStore(path) as store:
        assert store.append_ai_journal("case_a", ENTRY, NOW) == 1
        assert store.append_ai_journal("case_a", fallback, NOW) == 2
        assert store.append_ai_journal("case_b", ENTRY, NOW) == 1
    with SqliteTransactionStore(path) as store:
        assert store.ai_journal("case_a") == (ENTRY, fallback)
        database = store._require_open()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            database.execute("UPDATE pb_ai_journal SET entry_json='{}'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            database.execute("DELETE FROM pb_ai_journal")
        assert store.ai_journal("case_a") == (ENTRY, fallback)
        with pytest.raises(ValueError, match="subject"):
            store.append_ai_journal(" ", ENTRY, NOW)


def test_journal_failure_is_reported_without_raising(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "journal.sqlite") as store:
        database = store._require_open()
        database.execute("DROP TRIGGER pb_ai_journal_no_delete")
        database.execute("DROP TABLE pb_ai_journal")
        assert store.try_append_ai_journal("case_a", ENTRY, NOW) is False
        assert not database.in_transaction


def test_journal_never_rolls_back_a_caller_transaction(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "journal.sqlite") as store:
        database = store._require_open()
        database.execute("BEGIN IMMEDIATE")
        database.execute("INSERT INTO pb_store_versions VALUES ('caller_work', 1)")
        assert store.try_append_ai_journal("case_a", ENTRY, NOW) is False
        assert database.in_transaction
        database.execute("COMMIT")
        assert database.execute(
            "SELECT version FROM pb_store_versions WHERE component='caller_work'"
        ).fetchone() == (1,)
        assert store.ai_journal("case_a") == ()


_DECISION_MODULES = (
    "case_state.py",
    "payment_state.py",
    "payment_coordinator.py",
    "payment_records.py",
    "stripe_payment.py",
    "submission_coordinator.py",
    "submit_gate.py",
    "lob_submission.py",
    "lob_client.py",
    "lob_webhook.py",
    "effect_dispatch.py",
    "case_service.py",
    "returned_mail.py",
    "oplog/appeal_events.py",
)


def test_money_mail_and_report_code_never_reads_the_journal() -> None:
    root = Path(parkingbreaker.__file__).parent
    for name in _DECISION_MODULES:
        tree = ast.parse((root / name).read_text())
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        reads = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in {"ai_journal", "ai_calls"}
        }
        # case_service writes research usage to the journal and call log; nothing here reads.
        assert not reads, name
        if name != "case_service.py":
            assert (
                not {
                    "parkingbreaker.oplog.ai_journal",
                    "parkingbreaker.ai_journal_store",
                }
                & imported
            ), name
