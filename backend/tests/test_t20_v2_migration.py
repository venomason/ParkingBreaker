"""Real v2 persisted envelopes retain receipt identity and final dedup on upgrade."""

import json
import sqlite3
from pathlib import Path

from parkingbreaker.transaction_store import SqliteTransactionStore
from parkingbreaker.webhook_backlog_store import UNMATCHED_EXPIRED
from test_submission_coordinator import NOW

V2_DDL = """CREATE TABLE pb_webhook_backlog (
    provider TEXT NOT NULL CHECK (provider = 'lob'),
    event_id TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    received_at TEXT NOT NULL, letter_id TEXT, submission_token TEXT,
    PRIMARY KEY(provider, event_id)
)"""


def test_v2_upgrade_preserves_envelopes_final_ids_and_fairness_across_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v2.sqlite"
    payloads = {
        event: json.dumps({"id": event, "body": {"id": "ltr_" + event}})
        for event in ("evt_later", "evt_first", "evt_final")
    }
    with SqliteTransactionStore(path):
        pass
    with sqlite3.connect(path) as raw:
        raw.execute("DROP TABLE pb_webhook_backlog")
        raw.execute("DROP TABLE IF EXISTS pb_webhook_final_events")
        raw.execute(V2_DDL)
        raw.execute("CREATE INDEX pb_webhook_backlog_letter ON pb_webhook_backlog(letter_id)")
        raw.execute("CREATE INDEX pb_webhook_backlog_token ON pb_webhook_backlog(submission_token)")
        raw.execute("CREATE INDEX pb_webhook_backlog_received ON pb_webhook_backlog(received_at)")
        raw.execute("UPDATE pb_store_versions SET version=2 WHERE component='webhook_backlog'")
        for event, at in (
            ("evt_later", "2026-10-20T00:00:00+00:00"),
            ("evt_first", NOW.isoformat()),
        ):
            raw.execute(
                "INSERT INTO pb_webhook_backlog VALUES ('lob', ?, ?, ?, ?, NULL)",
                (event, payloads[event], at, "ltr_" + event),
            )
        raw.execute(
            "INSERT INTO pb_webhook_unmatched VALUES ('lob', ?, ?, ?, ?, ?)",
            (
                "evt_final",
                payloads["evt_final"],
                UNMATCHED_EXPIRED,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
    with SqliteTransactionStore(path) as store:
        rows = store.backlog_batch(10)
        assert [row.event_id for row in rows] == ["evt_first", "evt_later"]
        assert {row.event_id: row.payload for row in rows} == {
            key: payloads[key] for key in ("evt_first", "evt_later")
        }
        store.defer_backlog(rows[0])
        original = rows[0]
        # Removing the retained diagnostic payload must not resurrect its event identity.
        store._require_open().execute("DELETE FROM pb_webhook_unmatched")
    with SqliteTransactionStore(path) as store:
        assert [row.event_id for row in store.backlog_batch(10)] == ["evt_later", "evt_first"]
        assert store.backlog_row("lob", "evt_first") == original
        assert (
            store.record_webhook(
                "lob",
                "evt_final",
                None,
                payloads["evt_final"],
                occurred_at=NOW,
                new_effect_id=lambda: "unused",
            )
            is False
        )
        assert store.backlog_row("lob", "evt_final") is None
