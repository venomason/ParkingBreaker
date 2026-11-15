"""T18 coverage gaps: stale-row guards and rollback-failure notes in the SQLite stores."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from parkingbreaker.intake_store import IntakeConflict
from parkingbreaker.mail_attempt_store import SqliteMailAttemptStore, StorageCorruption
from parkingbreaker.payment_records import mutation_key
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_address_store import ADDRESS, seed_intakes
from test_intake_store import store_with_workspace
from test_t18_partial_recovery import _row
from test_t18_storage import NOW, REQUEST, _inject, _legacy_database, _restore


class _RacingConnection:
    """Delegates to the real connection but lets one racing write land first, once."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        pattern: str,
        race: Callable[[sqlite3.Connection], None],
    ) -> None:
        self._connection = connection
        self.pattern = pattern
        self.race = race
        self.fired = False

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if not self.fired and self.pattern in sql:
            self.fired = True
            self.race(self._connection)
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _inject_race(
    store: SqliteMailAttemptStore,
    pattern: str,
    race: Callable[[sqlite3.Connection], None],
) -> sqlite3.Connection:
    inner = store._require_open()
    store._database = _RacingConnection(inner, pattern, race)  # type: ignore[assignment]
    return inner


def _tables(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    tables = (
        "pb_intake_records",
        "pb_intake_authorization_attempts",
        "pb_cases",
        "pb_pending_effects",
    )
    with sqlite3.connect(path) as database:
        return {t: database.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}


# --- 1. guarded row-count branches ---------------------------------------------------------


def test_complete_authorization_intake_revision_stale_writes_nothing(tmp_path: Path) -> None:
    """A revision change between the read and the guarded link UPDATE refuses and writes nothing."""
    path = tmp_path / "pb.sqlite"
    store = store_with_workspace(path)
    with store:
        intake_id, tier, amount = "intake_1", "CERTIFIED", 2999
        payment_method_id = "pm_first"
        key = mutation_key("authorization", intake_id, f"{tier}:{amount}", payment_method_id)
        request_json = json.dumps(
            {
                "amount_cents": amount,
                "currency": "usd",
                "metadata": {"case": intake_id, "tier": tier, "service_amount_cents": str(amount)},
                "payment_method_id": payment_method_id,
                "tier": tier,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        store.record_intake(intake_id, "ws_1", '{"i":1}', '{"p":1}', NOW)
        store.update_intake(intake_id, 1, selected_tier=tier, case_token=None)
        store.begin_authorization_attempt(
            intake_id, key, tier, amount, payment_method_id, request_json, NOW
        )
        before = _tables(path)

        def change_revision(inner: sqlite3.Connection) -> None:
            inner.execute(
                "UPDATE pb_intake_records SET revision = revision + 1 WHERE intake_id=?",
                (intake_id,),
            )

        inner = _inject_race(store, "UPDATE pb_intake_records SET case_token", change_revision)
        try:
            with pytest.raises(IntakeConflict, match="intake_revision_stale"):
                store.complete_authorization_attempt(
                    intake_id, key, "pi_Original123", NOW, NOW + timedelta(days=7)
                )
        finally:
            _restore(store, inner)
        assert _tables(path) == before

        # Without the racing write the same guarded call links the case.
        case = store.complete_authorization_attempt(
            intake_id, key, "pi_Original123", NOW, NOW + timedelta(days=7)
        )
        linked = store.get_intake(intake_id)
        assert linked is not None
        assert linked.case_token == case.case_token


def test_legacy_recovery_row_changed_underneath_writes_nothing(tmp_path: Path) -> None:
    """A racing writer between the legacy read and its guarded UPDATE refuses and writes nothing."""
    path = tmp_path / "cases.sqlite"
    _legacy_database(path)
    with SqliteMailAttemptStore(path) as store:
        before = _row(path)

        def fill_envelope(inner: sqlite3.Connection) -> None:
            inner.execute(
                "UPDATE pb_mail_attempts SET envelope_json=?, request_sha256=? "
                "WHERE case_token=? AND attempt=?",
                ('{"raced":1}', "f" * 64, "legacy-case", 1),
            )

        inner = _inject_race(store, "SET envelope_json = COALESCE", fill_envelope)
        try:
            with pytest.raises(
                StorageCorruption, match="legacy recovery did not update exactly one row"
            ):
                store.prepare("legacy-case", 1, REQUEST, NOW + timedelta(hours=1))
        finally:
            _restore(store, inner)
        assert _row(path) == before


# --- 2. rollback failures must not mask the original error ----------------------------------


def test_intake_rollback_failure_does_not_mask_original_error(tmp_path: Path) -> None:
    store = store_with_workspace(tmp_path / "pb.sqlite")
    with store:
        record = store.record_intake("case_1", "ws_1", '{"i":1}', '{"p":1}', NOW)
        inner = _inject(store)
        try:
            with pytest.raises(IntakeConflict, match="intake_revision_stale") as raised:
                store.update_intake(
                    "case_1", record.revision + 1, selected_tier="CERTIFIED", case_token=None
                )
            assert any("ROLLBACK also failed" in n for n in raised.value.__notes__)
        finally:
            _restore(store, inner)
        assert store.get_intake("case_1") == record


def test_address_rollback_failure_does_not_mask_original_error(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "cases.sqlite") as store:
        seed_intakes(store)
        saved = store.save_customer_address("case-1", "owner-1", ADDRESS, NOW, None)
        inner = _inject(store)
        try:
            with pytest.raises(IntakeConflict, match="address_revision_stale") as raised:
                store.save_customer_address("case-1", "owner-1", ADDRESS, NOW, saved.revision + 1)
            assert any("ROLLBACK also failed" in n for n in raised.value.__notes__)
        finally:
            _restore(store, inner)
        assert store.customer_address_revisions("case-1", "owner-1") == (saved,)
