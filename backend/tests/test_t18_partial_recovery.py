from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from mail_samples import make_request
from parkingbreaker.lob_submission import LetterRequest
from parkingbreaker.mail_attempt_store import PreparationConflict, SqliteMailAttemptStore
from test_t18_storage import NOW, REQUEST, _legacy_database


def _row(path: Path) -> tuple[object, ...]:
    with sqlite3.connect(path) as database:
        row = database.execute(
            """SELECT * FROM pb_mail_attempts
               WHERE case_token = 'legacy-case' AND attempt = 1"""
        ).fetchone()
    assert row is not None
    return tuple(row)


def _changed_requests() -> tuple[LetterRequest, ...]:
    return (
        replace(REQUEST, recipient=replace(REQUEST.recipient, line1="999 Changed Avenue")),
        replace(REQUEST, sender=replace(REQUEST.sender, line1="999 Changed Street")),
        replace(REQUEST, options=replace(REQUEST.options, color=True)),
    )


@pytest.mark.parametrize("missing", ["envelope_json", "request_sha256"])
@pytest.mark.parametrize("changed", _changed_requests())
def test_partial_recovery_refuses_changed_envelope_without_writing(
    tmp_path: Path, missing: str, changed: LetterRequest
) -> None:
    path = tmp_path / "cases.sqlite"
    _legacy_database(path)
    with SqliteMailAttemptStore(path) as store:
        store.prepare("legacy-case", 1, REQUEST, NOW + timedelta(hours=1))
    with sqlite3.connect(path) as database:
        database.execute(f"UPDATE pb_mail_attempts SET {missing} = NULL")
    before = _row(path)

    with SqliteMailAttemptStore(path) as store, pytest.raises(PreparationConflict):
        store.prepare("legacy-case", 1, changed, NOW + timedelta(hours=2))

    assert _row(path) == before


@pytest.mark.parametrize("missing", ["envelope_json", "request_sha256"])
def test_partial_recovery_fills_only_missing_field_and_keeps_identity(
    tmp_path: Path, missing: str
) -> None:
    path = tmp_path / "cases.sqlite"
    original = _legacy_database(path)
    with SqliteMailAttemptStore(path) as store:
        prepared = store.prepare("legacy-case", 1, REQUEST, NOW + timedelta(hours=1))
    with sqlite3.connect(path) as database:
        database.execute(f"UPDATE pb_mail_attempts SET {missing} = NULL")
    before = _row(path)

    with SqliteMailAttemptStore(path) as store:
        recovered = store.prepare("legacy-case", 1, REQUEST, NOW + timedelta(hours=2))

    after = _row(path)
    assert recovered.request == REQUEST
    assert recovered.prepared_at == NOW
    assert recovered.key == prepared.key
    assert after[:7] == original
    kept_index = 8 if missing == "envelope_json" else 7
    filled_index = 7 if missing == "envelope_json" else 8
    assert after[kept_index] == before[kept_index]
    assert after[filled_index] is not None
    assert recovered.prepared_at == prepared.prepared_at


@pytest.mark.parametrize(
    ("survivor", "corrupt_value"),
    [("envelope_json", "{"), ("request_sha256", "not-a-sha256")],
)
def test_partial_recovery_never_overwrites_malformed_surviving_field(
    tmp_path: Path, survivor: str, corrupt_value: str
) -> None:
    path = tmp_path / "cases.sqlite"
    _legacy_database(path)
    with SqliteMailAttemptStore(path) as store:
        store.prepare("legacy-case", 1, REQUEST, NOW)
    missing = "request_sha256" if survivor == "envelope_json" else "envelope_json"
    with sqlite3.connect(path) as database:
        database.execute(
            f"UPDATE pb_mail_attempts SET {survivor} = ?, {missing} = NULL", (corrupt_value,)
        )
    before = _row(path)

    with SqliteMailAttemptStore(path) as store, pytest.raises(PreparationConflict):
        store.prepare("legacy-case", 1, REQUEST, NOW + timedelta(hours=1))

    assert _row(path) == before


def test_complete_request_mismatch_remains_a_conflict(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    _legacy_database(path)
    with SqliteMailAttemptStore(path) as store:
        store.prepare("legacy-case", 1, REQUEST, NOW)
    before = _row(path)
    changed = make_request(
        REQUEST.pdf,
        REQUEST.submission_token,
    )
    changed = replace(changed, recipient=replace(changed.recipient, line2="Room 9"))

    with SqliteMailAttemptStore(path) as store, pytest.raises(PreparationConflict):
        store.prepare("legacy-case", 1, changed, NOW + timedelta(hours=1))

    assert _row(path) == before
