"""Read the three-price experiment from a ParkingBreaker transaction database."""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from parkingbreaker.prices import PRICE_ARMS, PRICE_EXPERIMENT_ID


def price_funnel(path: Path) -> list[dict[str, int | str]]:
    """One read-only snapshot. A visitor is one stored browser assignment."""
    query = """
        WITH offers AS (
            SELECT s.visitor_id, r.intake_id, r.case_token, v.arm, v.amount_cents
            FROM pb_price_visitors v
            JOIN pb_price_scopes s USING (visitor_id)
            JOIN pb_intake_owners o ON o.owner = s.scope_id
            JOIN pb_intake_records r USING (workspace_token)
            WHERE v.experiment_id = :experiment
              AND NOT EXISTS (SELECT 1 FROM pb_beta_entitlements b WHERE b.intake_id = r.intake_id)
              AND EXISTS (
                  SELECT 1 FROM json_each(r.prehold_json, '$.tiers') t
                  WHERE t.value ->> '$.tier' = 'CERTIFIED'
                    AND t.value ->> '$.amount_cents' = v.amount_cents
              )
        ),
        tagged AS (
            SELECT o.intake_id, MAX(c.case_token) AS held_case
            FROM offers o
            JOIN pb_intake_authorization_attempts a USING (intake_id)
            LEFT JOIN pb_cases c ON c.case_token = o.case_token
                AND c.authorization_ref = a.authorization_ref AND a.state = 'authorized'
                AND c.amount_cents = o.amount_cents
            WHERE a.amount_cents = o.amount_cents
              AND a.request_json ->> '$.metadata.price_experiment_id' = :experiment
              AND a.request_json ->> '$.metadata.price_arm' = o.arm
            GROUP BY o.intake_id
        )
        SELECT v.arm, COUNT(DISTINCT v.visitor_id) AS assigned_visitors,
               COUNT(o.intake_id) AS recorded_offers,
               COUNT(t.intake_id) AS checkout_appeals, COUNT(c.case_token) AS held_appeals,
               COUNT(CASE WHEN c.payment_state IN ('CAPTURED', 'REFUNDED') THEN 1 END)
                   AS captured_appeals,
               COUNT(CASE WHEN c.payment_state = 'REFUNDED' THEN 1 END) AS refunded_appeals,
               SUM(CASE WHEN c.payment_state IN ('CAPTURED', 'REFUNDED')
                        THEN c.amount_cents ELSE 0 END) AS gross_captured_cents
        FROM pb_price_visitors v
        LEFT JOIN offers o USING (visitor_id)
        LEFT JOIN tagged t USING (intake_id)
        LEFT JOIN pb_cases c ON c.case_token = t.held_case
        WHERE v.experiment_id = :experiment
        GROUP BY v.arm
    """
    with closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        cursor = db.execute(query, {"experiment": PRICE_EXPERIMENT_ID})
        counts = {row["arm"]: dict(row) for row in cursor}
        empty = {col[0]: 0 for col in cursor.description if col[0] != "arm"}
    return [
        {"arm": arm, "amount_cents": cents, **counts.get(arm, empty)}
        for arm, cents in PRICE_ARMS.items()
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    print(json.dumps(price_funnel(parser.parse_args().database), indent=2))
