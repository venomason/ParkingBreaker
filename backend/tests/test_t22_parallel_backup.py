"""Verify the backup dry-run interpreter selection without running recovery code."""

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = "-m parkingbreaker.recovery_cli backup --dry-run"


def _shim(path: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "{label} $*"\n')
    path.chmod(0o755)


def _run(tmp_path: Path, *, project: bool, override: bool) -> str:
    checkout = tmp_path / "checkout"
    deploy = checkout / "deploy"
    deploy.mkdir(parents=True)
    shutil.copy2(ROOT / "deploy/backup.sh", deploy / "backup.sh")
    path_bin = tmp_path / "path-bin"
    _shim(path_bin / "python3", "PATH_PYTHON")
    project_python = checkout / "backend/.venv/bin/python"
    if project:
        _shim(project_python, "PROJECT_PYTHON")
    override_path = tmp_path / "override-python"
    if override:
        _shim(override_path, "OVERRIDE_PYTHON")

    env = dict(os.environ, PATH=f"{path_bin}:/usr/bin:/bin")
    if override:
        env["PB_PYTHON"] = str(override_path)
    else:
        env.pop("PB_PYTHON", None)
    result = subprocess.run(
        ["bash", str(deploy / "backup.sh"), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_dry_run_prefers_executable_project_interpreter(tmp_path: Path) -> None:
    assert _run(tmp_path, project=True, override=False) == f"PROJECT_PYTHON {EXPECTED}"


def test_dry_run_explicit_interpreter_override_wins(tmp_path: Path) -> None:
    assert _run(tmp_path, project=True, override=True) == f"OVERRIDE_PYTHON {EXPECTED}"


def test_dry_run_falls_back_to_path_python3_without_project_interpreter(
    tmp_path: Path,
) -> None:
    assert _run(tmp_path, project=False, override=False) == f"PATH_PYTHON {EXPECTED}"
