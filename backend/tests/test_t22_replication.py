"""Optional off-site configuration stays disabled until target qualification."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_replication_overlay_is_nonrestoring_single_host_and_private(tmp_path: Path) -> None:
    env = tmp_path / "app.env"
    env.write_text(
        "PB_WORKER_SECRET=synthetic-worker-secret-0123456789\n"
        "PB_PROVIDER_MODE=fake\n"
        "PB_SERVICE_PHASE=controlled_free_beta\n"
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env),
            "-f",
            str(ROOT / "compose.yaml"),
            "-f",
            str(ROOT / "deploy/compose.production.yaml"),
            "-f",
            str(ROOT / "deploy/compose.replication.yaml"),
            "--profile",
            "offsite",
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=dict(
            os.environ,
            PB_ENV_FILE=str(env),
            PB_DOMAIN="offline.example.invalid",
            PB_DATA_PATH=str(tmp_path / "data"),
            PB_TLS_PATH=str(tmp_path / "tls"),
            PB_REPLICA_URL="gs://offline-fixture/never-created",
        ),
    )
    assert result.returncode == 0, result.stderr
    replica = json.loads(result.stdout)["services"]["litestream"]
    assert replica["profiles"] == ["offsite"]
    assert not replica.get("ports")
    assert replica["user"] == "10001:10001"
    assert replica["command"] == ["replicate", "-config", "/etc/litestream.yml"]
    config = (ROOT / "deploy/litestream.yml").read_text()
    assert "restore-if-db-not-exists: false" in config
    assert "auto-recover: false" in config
    assert "${PB_REPLICA_URL}" in config


def test_scheduled_backup_has_offline_dry_run() -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/backup.sh"), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ, PB_PYTHON=str(Path(sys.executable))),
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["restore"]["records_match"] is True
    assert report["letters"] == 1
    assert report["captures"] == 1


# ---- host backup script: same command bodies as --apply, recorded by command stand-ins ----


def test_private_backup_and_replica_use_the_active_private_stack(tmp_path: Path) -> None:
    from test_t22_lifecycle import commands, rehearsal, run_script

    replica = tmp_path / "offsite"
    replica.mkdir()
    env, state = rehearsal(
        tmp_path, PB_DEPLOY_TOPOLOGY="cloudrun-private-vm", PB_REPLICA_DIRECTORY=str(replica)
    )
    # Simulate the mounted transport. Docker is also a command recorder in this rehearsal.
    findmnt = tmp_path / "shims/findmnt"
    findmnt.write_text('#!/bin/sh\nprintf "%s\\n" "fuse.sshfs"\n')
    findmnt.chmod(0o755)
    result = run_script("backup.sh", "--rehearse", str(state), env=env)
    assert result.returncode == 0, result.stderr
    backup, copy = commands(tmp_path)
    deploy = Path(env["PB_RELEASE"]) / "deploy"
    overlay = f"-f {deploy}/compose.private-vm.yaml -f {deploy}/private-images.json"
    assert overlay in backup
    assert overlay in copy
    assert f"-v {replica}:/replica" in copy
    assert "findmnt --noheadings --mountpoint /replica" in copy
    assert "recovery_cli replicate" in copy
    assert "--record-off-host" in copy


def test_private_backup_keeps_local_bundle_and_alerts_without_offhost_mount(tmp_path: Path) -> None:
    from test_t22_lifecycle import commands, rehearsal, run_script

    replica = tmp_path / "ordinary-directory"
    replica.mkdir()
    env, state = rehearsal(
        tmp_path, PB_DEPLOY_TOPOLOGY="cloudrun-private-vm", PB_REPLICA_DIRECTORY=str(replica)
    )
    result = run_script("backup.sh", "--rehearse", str(state), env=env)
    assert result.returncode != 0
    (backup,) = commands(tmp_path)
    assert "recovery_cli backup" in backup
    assert "backup_job_failed" in (tmp_path / "data/recovery/recovery-alerts.jsonl").read_text()


def test_scheduled_backup_runs_the_recovery_cli_inside_the_api_container(tmp_path: Path) -> None:
    from test_t22_lifecycle import commands, rehearsal, run_script

    env, state = rehearsal(tmp_path)
    result = run_script("backup.sh", "--rehearse", str(state), env=env)
    assert result.returncode == 0, result.stderr
    (line,) = commands(tmp_path)
    assert "-p parkingbreaker" in line
    assert "exec -T api python -m parkingbreaker.recovery_cli backup --data /data" in line
    assert "--output /data/recovery/scheduled-" in line


def test_scheduled_backup_replicates_the_verified_bundle_to_an_independent_directory(
    tmp_path: Path,
) -> None:
    from test_t22_lifecycle import commands, rehearsal, run_script

    replica = tmp_path / "offsite-mount"
    replica.mkdir()
    env, state = rehearsal(tmp_path, PB_REPLICA_DIRECTORY=str(replica))
    result = run_script("backup.sh", "--rehearse", str(state), env=env)
    assert result.returncode == 0, result.stderr
    backup, copy = commands(tmp_path)
    name = backup.split("--output /data/recovery/")[1]
    assert f"-v {replica}:/replica" in copy
    assert f"recovery_cli replicate --data /data/recovery/{name} --output /replica/{name}" in copy
    assert "--record-off-host" not in copy

    link = tmp_path / "link"
    link.symlink_to(replica)
    (tmp_path / "second").mkdir()
    env, state = rehearsal(tmp_path / "second", PB_REPLICA_DIRECTORY=str(link))
    result = run_script("backup.sh", "--rehearse", str(state), env=env)
    assert result.returncode != 0
    assert not any(" replicate " in line for line in commands(tmp_path / "second"))


def test_backup_skips_without_touching_docker_while_a_lifecycle_holds_the_lock(
    tmp_path: Path,
) -> None:
    import fcntl

    from test_t22_lifecycle import commands, rehearsal, run_script

    env, state = rehearsal(tmp_path)
    with (state / "lifecycle.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = run_script("backup.sh", "--rehearse", str(state), env=env)
    assert result.returncode == 3
    assert "backup_skipped_lifecycle_active" in result.stderr
    assert commands(tmp_path) == []


def test_failed_host_backup_appends_an_alert_and_never_erases_recovery_evidence(
    tmp_path: Path,
) -> None:
    from test_t22_lifecycle import rehearsal, run_script

    env, state = rehearsal(tmp_path, SHIM_FAIL_MATCH="recovery_cli backup")
    recovery = tmp_path / "data" / "recovery"
    old_bundle = recovery / "scheduled-earlier"
    old_bundle.mkdir(parents=True)
    (old_bundle / "COMPLETE").write_text("earlier verified snapshot")
    alerts = recovery / "recovery-alerts.jsonl"
    alerts.write_text('{"event":"replication_failed"}\n')
    result = run_script("backup.sh", "--rehearse", str(state), env=env)
    assert result.returncode != 0
    assert "backup_job_failed" in result.stderr
    assert alerts.read_text().splitlines() == [
        '{"event":"replication_failed"}',
        '{"event":"backup_job_failed"}',
    ]
    assert (old_bundle / "COMPLETE").read_text() == "earlier verified snapshot"


def test_scheduled_backup_unit_cannot_hold_the_host_lock_forever() -> None:
    """A hung backup would block every deploy, so systemd must bound and report its run."""
    service = (ROOT / "deploy/systemd/parkingbreaker-backup.service").read_text()
    assert "Type=oneshot" in service
    assert "TimeoutStartSec=" in service
    assert "StandardError=journal" in service  # failures stay observable in journald
    assert "Restart=" not in service  # a failed run is alerted, never silently retried
    assert "ExecStart=/bin/bash ${PB_RELEASE}/deploy/backup.sh --apply" in service
    timer = (ROOT / "deploy/systemd/parkingbreaker-backup.timer").read_text()
    assert "Persistent=true" in timer
    assert "OnCalendar=hourly" in timer
    private = (ROOT / "deploy/systemd/parkingbreaker-backup-private.timer").read_text()
    assert "OnCalendar=*-*-* *:00,30:00" in private
    assert "Unit=parkingbreaker-backup.service" in private
    assert "Persistent=true" in private
