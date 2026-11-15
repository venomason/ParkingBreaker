#!/usr/bin/env python3
"""Read-only durable proof for the explicitly marked synthetic beta runtime.

Selection remains in beta-acceptance-fixtures.py. No state is seeded or changed here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True)
    args = parser.parse_args()
    guard = runpy.run_path(str(Path(__file__).with_name("beta-acceptance-fixtures.py")))
    if guard["CASE"].fullmatch(args.case_id) is None:
        guard["fail"]("case id is not a synthetic case id")
    _, database = guard["runtime_paths"]()
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        intake = connection.execute(
            "SELECT case_token FROM pb_intake_records WHERE intake_id=?", (args.case_id,)
        ).fetchone()
        if intake is None:
            guard["fail"]("synthetic intake does not exist")
        token = intake["case_token"]
        case = connection.execute(
            "SELECT payment_state, letter_state, amount_cents, attempt FROM pb_cases WHERE case_token=?", (token,)
        ).fetchone()
        def count(table: str, column: str = "case_token", value: str | None = token) -> int:
            return connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (value,)).fetchone()[0]
        attempts = [dict(row) for row in connection.execute(
            "SELECT attempt, submission_token, idempotency_key, pdf_sha256, pdf, request_sha256 FROM pb_mail_attempts WHERE case_token=? ORDER BY attempt", (token,)
        )]
        for attempt in attempts:
            attempt["stored_bytes_sha256"] = hashlib.sha256(attempt.pop("pdf")).hexdigest()
        operations = [dict(row) for row in connection.execute(
            "SELECT attempt, kind, outcome_json, completed_at FROM pb_submission_operations WHERE case_token=? ORDER BY started_at, operation_id", (token,)
        )]
        history = [dict(row) for row in connection.execute(
            "SELECT machine, event_name, target FROM pb_case_history WHERE case_token=? ORDER BY revision", (token,)
        )]
        print(json.dumps({
            "case_id": args.case_id,
            "case": None if case is None else dict(case),
            "payment_authorization_rows": count("pb_intake_authorization_attempts", "intake_id", args.case_id),
            "payment_operation_rows": count("pb_payment_operations"),
            "payment_cursor_rows": count("pb_payment_cursor"),
            "mail_attempts": attempts,
            "submission_operations": operations,
            "history": history,
        }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
