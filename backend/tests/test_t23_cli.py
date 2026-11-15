"""The owner-facing entry point must exercise a seeded recovery offline."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_cli_dry_run_exercises_restore_and_fakes(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "parkingbreaker.recovery_cli",
            "rehearse",
            "--output",
            str(tmp_path / "rehearsal"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["reconciliation"]["status"] == "READY_FAKE"
    assert report["restore"]["records_match"] is True
    assert report["restore"]["artifacts_match"] is True
    assert report["letters"] == 1
    assert report["captures"] == 1
    assert (tmp_path / "rehearsal/backup/manifest.json").is_file()


def test_cli_failure_is_observable_without_traceback_or_secret(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "parkingbreaker.recovery_cli",
            "backup",
            "--data",
            str(tmp_path / "missing"),
            "--output",
            str(tmp_path / "failed"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ, STRIPE_SECRET_KEY="do-not-print-this"),
    )
    assert result.returncode != 0
    assert "backup_failed" in result.stderr
    assert "Traceback" not in result.stderr
    assert "do-not-print-this" not in result.stderr
    assert (tmp_path / "failed/INCOMPLETE").is_file()
