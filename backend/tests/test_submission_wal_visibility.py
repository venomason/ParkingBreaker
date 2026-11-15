"""Durable-write visibility across submission ownership; synthetic scratch DBs only.

Regression for an ownership seam that opened and closed the main SQLite database
inode to take flock. Closing any separate fd for that inode drops this process's
SQLite POSIX locks, so a later fresh reader could checkpoint and remove the WAL and
then read fewer committed rows than the owning connection had acknowledged.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from parkingbreaker.transaction_store import RevisionConflict, SqliteTransactionStore

READER = """\
import json, sqlite3, sys
database = sqlite3.connect(sys.argv[1], isolation_level=None)
print(json.dumps(database.execute("SELECT value FROM scratch_probe ORDER BY value").fetchall()))
database.close()
"""


def fresh_read(path: Path) -> list[list[str]]:
    result = subprocess.run(
        [sys.executable, "-c", READER, str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return cast(list[list[str]], json.loads(result.stdout))


def test_submission_ownership_preserves_durable_wal_writes(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    wal = Path(f"{path}-wal")
    with SqliteTransactionStore(path) as store:
        database = store._require_open()
        database.execute("CREATE TABLE scratch_probe(value TEXT)")
        database.execute("INSERT INTO scratch_probe VALUES ('before')")
        with store.submission_ownership():
            assert fresh_read(path) == [["before"]]
            assert wal.exists()
        assert fresh_read(path) == [["before"]]
        assert wal.exists()
        database.execute("INSERT INTO scratch_probe VALUES ('after')")
        own = [
            list(row) for row in database.execute("SELECT value FROM scratch_probe ORDER BY value")
        ]
        assert own == [["after"], ["before"]]
        assert fresh_read(path) == own
        assert wal.exists()


def test_ownership_lock_survives_cursor_commits_and_visibility(tmp_path: Path) -> None:
    path = tmp_path / "cases.sqlite"
    with SqliteTransactionStore(path) as store:
        database = store._require_open()
        database.execute("CREATE TABLE scratch_probe(value TEXT)")
        with store.submission_ownership():
            database.execute("BEGIN")
            database.execute("INSERT INTO scratch_probe VALUES ('committed')")
            database.execute("COMMIT")
            assert fresh_read(path) == [["committed"]]
            with (
                SqliteTransactionStore(path) as peer,
                pytest.raises(RevisionConflict, match="owns"),
                peer.submission_ownership(),
            ):
                pass
        assert fresh_read(path) == [["committed"]]
