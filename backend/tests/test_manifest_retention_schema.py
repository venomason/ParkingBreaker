"""Known-table names do not waive schema or immutability checks during purge."""

from pathlib import Path

import pytest

import test_retention_plan as fixture
from parkingbreaker import approval_manifest as am
from parkingbreaker import retention_plan as rp
from parkingbreaker import retention_purge as purge


@pytest.mark.parametrize("trigger", am.APPROVAL_MANIFEST_TRIGGER_NAMES)
def test_purge_refuses_missing_approval_protection(tmp_path: Path, trigger: str) -> None:
    db = fixture.make_database(tmp_path / "schema.sqlite", case_workspace=True)
    try:
        am.initialize_approval_manifests(db)
        db.commit()
        purge._source_schema(db)
        db.execute(f'DROP TRIGGER "{trigger}"')
        with pytest.raises(purge.RetentionPurgeRefused, match="schema_drift"):
            purge._source_schema(db)
    finally:
        db.close()


def test_known_approval_table_with_extra_column_is_not_trusted(tmp_path: Path) -> None:
    db = fixture.make_database(tmp_path / "extra.sqlite", case_workspace=True)
    try:
        am.initialize_approval_manifests(db)
        db.commit()
        db.execute("ALTER TABLE pb_approval_manifests ADD COLUMN unknown_customer_payload TEXT")
        states = rp._schema_snapshot(db)[0]
        assert states["pb_approval_manifests"] is rp.SourceState.MALFORMED
        plan = fixture.build_plan(db)
        assert rp.PlanBlockedReason.SOURCE_SCHEMA_MALFORMED.value in plan.summary.blocked_reasons
        with pytest.raises(purge.RetentionPurgeRefused, match="schema_drift"):
            purge._source_schema(db)
    finally:
        db.close()
