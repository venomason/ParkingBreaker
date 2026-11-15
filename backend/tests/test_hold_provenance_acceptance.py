"""Independent malformed-history and late-evidence acceptance controls."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from parkingbreaker import case_state as cs
from parkingbreaker.case_state import CaseState
from parkingbreaker.lob_submission import Accepted, LetterEvidence
from parkingbreaker.mail_attempt_store import StorageCorruption
from parkingbreaker.payment_state import PaymentState
from parkingbreaker.transaction_store import SqliteTransactionStore
from test_beta_admission import NOW
from test_beta_dispatch_timing import ORIGINAL, TOKEN, Beta
from test_submission_provenance_migration import _build_v1
from test_t74_mail_hold_boundary import seed_hold


def upgraded(path: Path) -> str:
    _build_v1(path)
    with SqliteTransactionStore(path) as store:
        return store.case_ids()[0]


@pytest.mark.parametrize("marker", [2, -1, "invalid"])
def test_runtime_invalid_marker_is_not_hidden_by_canonical_check(
    tmp_path: Path, marker: int | str
) -> None:
    path = tmp_path / "invalid-marker.sqlite"
    token = upgraded(path)
    with sqlite3.connect(path) as database:
        # Simulate stored corruption while retaining the real schema declaration.
        database.execute("PRAGMA ignore_check_constraints=ON")
        database.execute("UPDATE pb_submission_operations SET temporary_lookup=?", (marker,))
        database.execute("PRAGMA ignore_check_constraints=OFF")
    with (
        pytest.raises(StorageCorruption, match="invalid temporary lookup marker"),
        SqliteTransactionStore(path) as store,
    ):
        store.submission_cursor(token, 1)


def test_runtime_temporary_marker_on_create_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "invalid-create-marker.sqlite"
    token = upgraded(path)
    with sqlite3.connect(path) as database:
        database.execute("PRAGMA ignore_check_constraints=ON")
        database.execute("UPDATE pb_submission_operations SET temporary_lookup=1")
        database.execute("PRAGMA ignore_check_constraints=OFF")
    with (
        pytest.raises(StorageCorruption, match="temporary marker on a create"),
        SqliteTransactionStore(path) as store,
    ):
        store.submission_cursor(token, 1)


@pytest.mark.parametrize("mutation", ["type", "nullable", "check"])
def test_marker_schema_drift_fails_closed(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / "invalid-schema.sqlite"
    upgraded(path)
    with sqlite3.connect(path) as database:
        ddl = database.execute(
            "SELECT sql FROM sqlite_master WHERE name='pb_submission_operations'"
        ).fetchone()[0]
        if mutation == "type":
            changed = ddl.replace("temporary_lookup INTEGER", "temporary_lookup TEXT")
        elif mutation == "nullable":
            changed = ddl.replace("temporary_lookup INTEGER NOT NULL", "temporary_lookup INTEGER")
        else:
            changed = ddl.replace("temporary_lookup IN (0, 1)", "temporary_lookup IN (0, 1, 2)")
        assert changed != ddl
        database.execute("ALTER TABLE pb_submission_operations RENAME TO old_operations")
        database.execute(changed)
        database.execute("INSERT INTO pb_submission_operations SELECT * FROM old_operations")
        database.execute("DROP TABLE old_operations")
    with pytest.raises(StorageCorruption, match="pb_submission_operations"):
        SqliteTransactionStore(path)


def test_held_case_without_cursor_can_project_original_provider_truth(tmp_path: Path) -> None:
    beta = Beta(tmp_path / "late-evidence.sqlite")
    try:
        case = beta._post(beta.case, cs.SubmitRequested())
        assert beta.store.submission_cursor(beta.token, 1) is None
        packet = beta.store.get(beta.token, 1)
        assert packet is not None
        hold = seed_hold(beta.store, beta.token)
        # The original provider object already exists; no new physical create is issued.
        beta.lob.letters[TOKEN] = ORIGINAL
        with beta.store.submission_ownership():
            claimed, _kind = beta.store.claim_submission(
                beta.token,
                1,
                case.revision,
                None,
                now=NOW,
                operation_id=beta.ids(),
                evidence=True,
            )
            beta.store.finish_submission(
                claimed,
                Accepted(ORIGINAL, evidence=LetterEvidence.PRODUCTION),
                now=NOW,
                effect_ids=(),
                new_effect_id=beta.ids,
            )
        assert beta.case.letter_state is CaseState.IN_TRANSIT
        assert beta.case.payment_state is PaymentState.NO_CHARGE
        assert beta.lob.calls == []
        assert beta.store.payment_cursor(beta.token) is None
        assert beta.store._require_open().execute(
            "SELECT COUNT(*) FROM pb_payment_operations"
        ).fetchone() == (0,)
        assert beta.store.case_hold(beta.token) == hold
        assert beta.store.get(beta.token, 1) == packet
        beta.store.close()
        with SqliteTransactionStore(beta.path) as reopened:
            assert reopened.case_hold(beta.token) == hold
            cursor = reopened.submission_cursor(beta.token, 1)
            assert cursor is not None
            assert isinstance(cursor.outcome, Accepted)
            assert cursor.outcome.letter_id == ORIGINAL
    finally:
        beta.store.close()
