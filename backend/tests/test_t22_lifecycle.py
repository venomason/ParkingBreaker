"""Exercise the owner scripts offline.

`lifecycle.sh --rehearse` runs the very command bodies of `--apply` (compose project
names, files, ordering, health loop) against recording shims for docker, gcloud and curl
placed first on PATH, with state in a temp directory. Only the host facts that need a
real root-owned env file and mounted regional disk are left to `validate.sh`.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from test_t22_cloudrun_overlay import write_image_lock

ROOT = Path(__file__).resolve().parents[2]

SHIM = """#!/usr/bin/env bash
# Recording stand-in: never talks to Docker, GCP or the network.
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$SHIM_LOG"
line="$(basename "$0") $*"
if [[ ${1:-} == ps && -n ${SHIM_PS_OUTPUT:-} ]]; then cat "$SHIM_PS_OUTPUT"; fi
if [[ $(basename "$0") == gcloud ]]; then printf '{}\\n'; fi
if [[ $line == "docker image inspect"* ]]; then
  printf '%s\\n' "${SHIM_IMAGE_COMMIT:-$PB_BUILD_COMMIT}"
fi
if [[ -n ${SHIM_FAIL_MATCH:-} && $line == *"$SHIM_FAIL_MATCH"* ]]; then exit 1; fi
if [[ $(basename "$0") == curl ]]; then
  printf '%s\\n%s' "$SHIM_PUBLIC_HEALTH" "${SHIM_HTTP_STATUS:-200}"
fi
if [[ $line == *"exec -T api"* && $line == *"/health"* ]]; then
  printf '%s\\n200' "$SHIM_LOCAL_HEALTH"
fi
# Readiness can be made to fail a configured number of times, then report real evidence.
if [[ $line == *release_readiness* && -n ${SHIM_READINESS_FAILS:-} ]]; then
  count=$(cat "$SHIM_STATE/readiness" 2>/dev/null || echo 0)
  echo $((count + 1)) > "$SHIM_STATE/readiness"
  (( count >= SHIM_READINESS_FAILS )) || exit 1
fi
# Warm-up: the first N health probes of the API container fail.
if [[ $line == *"exec -T api"* && -n ${SHIM_WARMUP:-} ]]; then
  count=$(cat "$SHIM_STATE/warmup" 2>/dev/null || echo 0)
  echo $((count + 1)) > "$SHIM_STATE/warmup"
  (( count >= SHIM_WARMUP )) || exit 1
fi
exit 0
"""


def run_script(
    name: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ROOT / "deploy" / name), *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def rehearsal(tmp_path: Path, **extra: str) -> tuple[dict[str, str], Path]:
    """Environment with command shims and temp paths; returns it and the state directory."""
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    for name in ("docker", "gcloud", "curl"):
        (shim_dir / name).write_text(SHIM)
        (shim_dir / name).chmod(0o755)
    for folder in ("data", "tls", "old", "mount", "state"):
        (tmp_path / folder).mkdir()
    commit = "1" * 40
    health = json.dumps({"status": "ok", "build_commit": commit, "mode": "fake"})
    env = dict(
        os.environ,
        PATH=f"{shim_dir}:{os.environ['PATH']}",
        SHIM_LOG=str(tmp_path / "shim.log"),
        SHIM_STATE=str(tmp_path),
        PB_RELEASE=str(ROOT),
        PB_OLD_RELEASE=str(tmp_path / "old"),
        PB_OLD_PROJECT="pb-previous",
        PB_ENV_FILE=str(tmp_path / "app.env"),
        PB_DATA_PATH=str(tmp_path / "data"),
        PB_TLS_PATH=str(tmp_path / "tls"),
        PB_DISK_NAME="pb-data",
        PB_VM_ID="https://compute.example.invalid/vm",
        PB_DISK_MOUNT=str(tmp_path / "mount"),
        PB_DISK_DEVICE="/dev/disk/by-id/google-pb-data",
        PB_DOMAIN="offline.example.invalid",
        PB_BUILD_COMMIT=commit,
        PB_PROVIDER_MODE="fake",
        SHIM_LOCAL_HEALTH=health,
        SHIM_PUBLIC_HEALTH=health,
    )
    env.update(extra)
    if env.get("PB_DEPLOY_TOPOLOGY") == "cloudrun-private-vm":
        env["PB_RELEASE"] = str(tmp_path / "candidate")
        write_image_lock(Path(env["PB_RELEASE"]) / "deploy/private-images.json")
    return env, tmp_path / "state"


def commands(tmp_path: Path) -> list[str]:
    log = tmp_path / "shim.log"
    return log.read_text().splitlines() if log.exists() else []


def index(lines: list[str], *needles: str) -> int:
    for position, line in enumerate(lines):
        if all(needle in line for needle in needles):
            return position
    raise AssertionError(f"no command containing {needles}: {lines}")


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("provision-host.sh", ("--dry-run",)),
        ("lifecycle.sh", ("deploy", "--dry-run")),
        ("lifecycle.sh", ("rollback", "--dry-run")),
        ("validate.sh", ("--dry-run",)),
    ],
)
def test_every_owner_script_has_offline_mode(name: str, args: tuple[str, ...]) -> None:
    result = run_script(name, *args)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


def test_private_host_prepares_only_the_frontend_gateway_and_iap() -> None:
    env = dict(
        os.environ,
        PB_DEPLOY_TOPOLOGY="cloudrun-private-vm",
        PB_FRONTEND_SUBNET_CIDR="10.44.1.0/26",
        PB_PRIVATE_VM_IP="10.44.2.4",
    )
    result = run_script("provision-host.sh", "--dry-run", env=env)
    assert result.returncode == 0, result.stderr
    assert "ufw allow from 10.44.1.0/26 to 10.44.2.4 port 8443 proto tcp" in result.stdout
    assert "ufw allow from 35.235.240.0/20 to any port 22 proto tcp" in result.stdout
    assert "allow 80/tcp" not in result.stdout
    assert "allow 443/tcp" not in result.stdout


@pytest.mark.parametrize(
    ("cidr", "vm"),
    [
        ("0.0.0.0/0", "10.44.2.4"),
        ("10.44.1.2/26", "10.44.2.4"),
        ("10.44.1.0/26", "0.0.0.0"),
        ("10.44.1.0/26", "127.0.0.1"),
        ("", "10.44.2.4"),
    ],
)
def test_private_host_refuses_invalid_gateway_before_preparing_rules(cidr: str, vm: str) -> None:
    env = dict(
        os.environ,
        PB_DEPLOY_TOPOLOGY="cloudrun-private-vm",
        PB_FRONTEND_SUBNET_CIDR=cidr,
        PB_PRIVATE_VM_IP=vm,
    )
    result = run_script("provision-host.sh", "--dry-run", env=env)
    assert result.returncode != 0
    assert "ufw allow" not in result.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="proves the non-root refusal")
@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("provision-host.sh", ("--apply",)),
        ("provision-host.sh", ("--enable-firewall",)),
        ("provision-host.sh", ("--install-backup-timer",)),
        ("lifecycle.sh", ("deploy", "--apply")),
        ("lifecycle.sh", ("rollback", "--apply")),
        ("backup.sh", ("--apply",)),
    ],
)
def test_root_changes_are_refused_for_an_unprivileged_caller(
    tmp_path: Path, name: str, args: tuple[str, ...]
) -> None:
    env, _state = rehearsal(tmp_path)
    result = run_script(name, *args, env=env)
    assert result.returncode == 2
    assert commands(tmp_path) == []  # no docker/gcloud/curl was reached


def test_cutover_stops_before_migration_and_failure_never_starts(tmp_path: Path) -> None:
    env, state = rehearsal(tmp_path)
    result = run_script("lifecycle.sh", "deploy", "--rehearse", str(state), env=env)
    assert result.returncode == 0, result.stderr
    events = (state / "events").read_text().splitlines()
    assert events.index("stop scheduler") < events.index("down old") < events.index("migrate")
    assert events.index("migrate") < events.index("start api web caddy")
    # Scheduler start alone is not enough: the last step must be the readiness proof.
    assert events[-2:] == ["start scheduler", "readiness"]
    lines = commands(tmp_path)
    assert index(lines, "release_readiness") > index(lines, "up -d --no-deps scheduler")
    assert any(
        "release_readiness" in line and "--not-before" in line and "exec -T monitor" in line
        for line in lines
    )

    failed_dir = tmp_path / "failed"
    failed_dir.mkdir()
    env, failed = rehearsal(failed_dir, SHIM_FAIL_MATCH="SqliteTransactionStore")
    result = run_script("lifecycle.sh", "deploy", "--rehearse", str(failed), env=env)
    assert result.returncode != 0
    assert "start api web caddy" not in (failed / "events").read_text()
    assert (failed / "PROCESSING_BLOCKED").exists()
    assert not any("up -d" in line for line in commands(failed_dir))


def test_cutover_issues_scheduler_then_old_stack_then_migration_commands(tmp_path: Path) -> None:
    env, state = rehearsal(tmp_path)
    (tmp_path / "data" / "parkingbreaker.sqlite").write_bytes(b"existing authority")
    result = run_script("lifecycle.sh", "deploy", "--rehearse", str(state), env=env)
    assert result.returncode == 0, result.stderr
    lines = commands(tmp_path)
    old = (
        f"-p pb-previous -f {tmp_path}/old/compose.yaml "
        f"-f {tmp_path}/old/deploy/compose.production.yaml"
    )
    new = f"-p parkingbreaker -f {ROOT}/compose.yaml -f {ROOT}/deploy/compose.production.yaml"
    build = index(lines, new, "build api worker web monitor")
    stop_scheduler = index(lines, old, "stop scheduler")
    down_old = index(lines, old, "down --remove-orphans")
    exclusive = index(lines, "docker ps -q", "service=api")
    snapshot = index(lines, new, "recovery_cli backup")
    migrate = index(lines, new, "run --rm --no-deps", "SqliteTransactionStore")
    start = index(lines, new, "up -d --no-deps api worker web caddy")
    health = index(lines, new, "exec -T api")
    start_scheduler = index(lines, new, "up -d --no-deps scheduler")
    readiness = index(lines, new, "release_readiness")
    assert (
        build
        < stop_scheduler
        < down_old
        < exclusive
        < snapshot
        < migrate
        < start
        < health
        < start_scheduler
        < readiness
    )
    assert "--data /data --state /monitor --not-before" in lines[readiness]
    assert re.search(r"--not-before \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z", lines[readiness])
    assert index(lines, "docker ps -q", "service=scheduler") < snapshot
    # The worker shares the SQLite file, so an old worker must be gone too.
    assert down_old < index(lines, "docker ps -q", "service=worker") < snapshot
    # Never remove customer data with the old stack, never leave the candidate project bare.
    assert not any(re.search(r"\bdown\b.*( -v\b|--volumes)", line) for line in lines)
    health_url = "https://offline.example.invalid/api/health"
    assert any(line.startswith("curl ") and health_url in line for line in lines)


def test_first_deploy_without_a_database_skips_the_snapshot_but_not_the_ordering(
    tmp_path: Path,
) -> None:
    env, state = rehearsal(tmp_path)
    result = run_script("lifecycle.sh", "deploy", "--rehearse", str(state), env=env)
    assert result.returncode == 0, result.stderr
    assert not any("recovery_cli" in line for line in commands(tmp_path))
    assert any("SqliteTransactionStore" in line for line in commands(tmp_path))


def test_a_running_api_container_blocks_migration_and_start(tmp_path: Path) -> None:
    """Never two SQLite owners: any API or scheduler container still alive stops the cutover."""
    ps_output = tmp_path / "ps-output"
    ps_output.write_text("0123456789ab\n")
    env, state = rehearsal(tmp_path, SHIM_PS_OUTPUT=str(ps_output))
    result = run_script("lifecycle.sh", "deploy", "--rehearse", str(state), env=env)
    assert result.returncode != 0
    lines = commands(tmp_path)
    assert not any("run --rm" in line or "up -d" in line for line in lines)
    assert "migrate" not in (state / "events").read_text().splitlines()
    assert (state / "PROCESSING_BLOCKED").exists()


@pytest.mark.parametrize("action", ["deploy", "rollback"])
@pytest.mark.parametrize("role", ["api", "worker", "scheduler"])
def test_cannot_cut_over_when_docker_cannot_list_old_owners(
    tmp_path: Path, action: str, role: str
) -> None:
    env, state = rehearsal(tmp_path, SHIM_FAIL_MATCH=f"service={role}")
    (tmp_path / "data/parkingbreaker.sqlite").touch()
    result = run_script("lifecycle.sh", action, "--rehearse", str(state), env=env)
    assert result.returncode != 0
    assert (state / "events").read_text().splitlines()[-1] == "assert exclusive"
    assert (state / "PROCESSING_BLOCKED").exists()
    assert not any("run --rm" in line or "up -d" in line for line in commands(tmp_path))


@pytest.mark.parametrize("marker", ["RESTORE_QUARANTINE.json"])
def test_quarantined_data_is_refused_before_anything_is_stopped_or_opened(
    tmp_path: Path, marker: str
) -> None:
    env, state = rehearsal(tmp_path)
    (tmp_path / "data" / marker).write_text("{}")
    (tmp_path / "data" / "parkingbreaker.sqlite").write_bytes(b"restored, unreconciled")
    result = run_script("lifecycle.sh", "deploy", "--rehearse", str(state), env=env)
    assert result.returncode == 4
    assert "reconciliation" in result.stderr
    events = (state / "events").read_text().splitlines()
    assert "stop scheduler" not in events
    assert "migrate" not in events
    lines = commands(tmp_path)
    assert not any(
        word in line
        for line in lines
        for word in (" stop scheduler", " down ", " run --rm", " up -d")
    )
    assert (tmp_path / "data" / "parkingbreaker.sqlite").read_bytes() == b"restored, unreconciled"


def test_inputs_needed_after_the_old_stack_stops_are_required_up_front(tmp_path: Path) -> None:
    for missing in ("PB_DOMAIN", "PB_TLS_PATH"):
        case = tmp_path / missing
        case.mkdir()
        env, state = rehearsal(case)
        del env[missing]
        result = run_script("lifecycle.sh", "deploy", "--rehearse", str(state), env=env)
        assert result.returncode != 0
        assert missing in result.stderr
        assert commands(case) == []  # refused before the old stack was touched


def test_rollback_never_replaces_database_with_snapshot(tmp_path: Path) -> None:
    env, state = rehearsal(tmp_path)
    (tmp_path / "data" / "parkingbreaker.sqlite").write_bytes(b"current authority")
    result = run_script("lifecycle.sh", "rollback", "--rehearse", str(state), env=env)
    assert result.returncode == 0, result.stderr
    events = (state / "events").read_text().splitlines()
    assert "compatibility check" in events
    assert "migrate" not in events
    assert "restore snapshot" not in events
    lines = commands(tmp_path)
    assert any("/tmp/compat.sqlite" in line for line in lines)  # old schema runs on a copy
    authority_open = 'SqliteTransactionStore(Path("/data/parkingbreaker.sqlite"))'
    assert not any(authority_open in line for line in lines)
    assert (tmp_path / "data" / "parkingbreaker.sqlite").read_bytes() == b"current authority"


def test_concurrent_lifecycle_is_refused_before_any_step(tmp_path: Path) -> None:
    import fcntl

    env, state = rehearsal(tmp_path)
    with (state / "lifecycle.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = run_script("lifecycle.sh", "deploy", "--rehearse", str(state), env=env)
    assert result.returncode == 3
    assert not (state / "events").exists()
    assert commands(tmp_path) == []


def test_health_warmup_retries_before_enabling_scheduler(tmp_path: Path) -> None:
    env, state = rehearsal(tmp_path, SHIM_WARMUP="2")
    result = run_script("lifecycle.sh", "deploy", "--rehearse", str(state), env=env)
    assert result.returncode == 0, result.stderr
    events = (state / "events").read_text().splitlines()
    assert events.count("health") == 3
    assert events[-2:] == ["start scheduler", "readiness"]
    lines = commands(tmp_path)
    readiness = index(lines, "release_readiness")
    assert readiness > index(lines, "up -d --no-deps scheduler")
    assert "--not-before" in lines[readiness]


def test_health_failure_stops_candidate_and_keeps_processing_blocked(tmp_path: Path) -> None:
    env, state = rehearsal(tmp_path, SHIM_FAIL_MATCH="curl")
    result = run_script("lifecycle.sh", "deploy", "--rehearse", str(state), env=env)
    assert result.returncode != 0
    events = (state / "events").read_text().splitlines()
    assert "stop candidate" in events
    assert "start scheduler" not in events
    assert (state / "PROCESSING_BLOCKED").exists()
    lines = commands(tmp_path)
    assert index(lines, "stop scheduler api worker web caddy") > index(lines, "up -d --no-deps api")
    assert not any("up -d --no-deps scheduler" in line for line in lines)


@pytest.mark.parametrize("action", ["deploy", "rollback"])
def test_failed_readiness_after_healthy_http_blocks_both_paths(tmp_path: Path, action: str) -> None:
    """Healthy API/web/TLS still proves nothing without fresh runtime evidence."""
    env, state = rehearsal(tmp_path, SHIM_FAIL_MATCH="release_readiness")
    (tmp_path / "data" / "parkingbreaker.sqlite").write_bytes(b"existing authority")
    result = run_script("lifecycle.sh", action, "--rehearse", str(state), env=env)
    assert result.returncode != 0
    events = (state / "events").read_text().splitlines()
    assert "health" in events
    assert "start scheduler" in events
    assert events[-1] == "stop candidate"
    assert (state / "PROCESSING_BLOCKED").exists()
    assert index(commands(tmp_path), "exec -T monitor", "release_readiness") > index(
        commands(tmp_path), "up -d --no-deps scheduler"
    )


@pytest.mark.parametrize("action", ["deploy", "rollback"])
def test_missing_scheduler_blocks_and_stops_the_candidate(tmp_path: Path, action: str) -> None:
    env, state = rehearsal(tmp_path, SHIM_FAIL_MATCH="up -d --no-deps scheduler")
    (tmp_path / "data" / "parkingbreaker.sqlite").write_bytes(b"existing authority")
    result = run_script("lifecycle.sh", action, "--rehearse", str(state), env=env)
    assert result.returncode != 0
    events = (state / "events").read_text().splitlines()
    assert "readiness" not in events
    assert events[-1] == "stop candidate"
    assert (state / "PROCESSING_BLOCKED").exists()


@pytest.mark.parametrize("action", ["deploy", "rollback"])
def test_readiness_polling_is_bounded_and_never_unblocks(tmp_path: Path, action: str) -> None:
    env, state = rehearsal(tmp_path, SHIM_READINESS_FAILS="100")
    (tmp_path / "data" / "parkingbreaker.sqlite").write_bytes(b"existing authority")
    result = run_script("lifecycle.sh", action, "--rehearse", str(state), env=env)
    assert result.returncode == 6
    events = (state / "events").read_text().splitlines()
    assert events.count("readiness") == 90  # the configured bounded budget, not forever
    assert events[-1] == "stop candidate"
    assert (state / "PROCESSING_BLOCKED").exists()


@pytest.mark.parametrize("action", ["deploy", "rollback"])
def test_two_failed_readiness_polls_then_actual_success_unblocks(
    tmp_path: Path, action: str
) -> None:
    env, state = rehearsal(tmp_path, SHIM_READINESS_FAILS="2")
    (tmp_path / "data" / "parkingbreaker.sqlite").write_bytes(b"existing authority")
    result = run_script("lifecycle.sh", action, "--rehearse", str(state), env=env)
    assert result.returncode == 0, result.stderr
    events = (state / "events").read_text().splitlines()
    assert events.count("readiness") == 3
    assert events[events.index("start scheduler") + 1] == "readiness"
    assert events[-1] == "readiness"
    assert "stop candidate" not in events
    assert not (state / "PROCESSING_BLOCKED").exists()
    assert (tmp_path / "readiness").read_text().strip() == "3"


def test_no_script_creates_or_changes_a_billed_cloud_resource() -> None:
    """The only cloud command is a read-only describe of the already existing regional disk."""
    forbidden = re.compile(r"\b(terraform|gsutil|aws|az|kubectl|pulumi|bq)\b")
    for script in sorted((ROOT / "deploy").glob("*.sh")):
        for line in script.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            assert not forbidden.search(line), (script.name, line)
            if "gcloud" in line:
                assert re.search(r"gcloud compute disks describe ", line), (script.name, line)


@pytest.mark.parametrize("action", ["deploy", "rollback"])
def test_private_vm_lifecycle_uses_overlay_without_local_web(tmp_path: Path, action: str) -> None:
    env, state = rehearsal(
        tmp_path,
        PB_DEPLOY_TOPOLOGY="cloudrun-private-vm",
        PB_PUBLIC_ORIGIN="https://offline.example.invalid",
        PB_PRIVATE_VM_IP="10.40.0.2",
        PB_PRIVATE_API_HOST="api.private.invalid",
        PB_PRIVATE_TLS_PATH=str(tmp_path / "tls"),
    )
    (tmp_path / "data/parkingbreaker.sqlite").touch()
    result = run_script("lifecycle.sh", action, "--rehearse", str(state), env=env)
    assert result.returncode == 0, result.stderr
    lines = commands(tmp_path)
    compose = [line for line in lines if line.startswith("docker compose ")]
    assert compose
    assert all("compose.private-vm.yaml" in line for line in compose)
    assert not any("exec -T web" in line or "build api worker web" in line for line in lines)
    assert index(lines, "pull api worker monitor caddy scheduler") < index(lines, "stop scheduler")
    assert index(lines, "docker image inspect") < index(lines, "stop scheduler")
    assert all(" build " not in line for line in compose)
    assert any("up -d --no-deps api worker caddy monitor" in line for line in lines)
    assert any(line.startswith("curl ") and "/api/health" in line for line in lines)
    assert any("release_readiness" in line for line in lines)
    assert not (state / "PROCESSING_BLOCKED").exists()


def test_private_vm_frontend_health_failure_keeps_processing_blocked(tmp_path: Path) -> None:
    env, state = rehearsal(
        tmp_path,
        PB_DEPLOY_TOPOLOGY="cloudrun-private-vm",
        PB_PUBLIC_ORIGIN="https://offline.example.invalid",
        PB_PRIVATE_VM_IP="10.40.0.2",
        PB_PRIVATE_API_HOST="api.private.invalid",
        PB_PRIVATE_TLS_PATH=str(tmp_path / "tls"),
        SHIM_FAIL_MATCH="curl ",
    )
    result = run_script("lifecycle.sh", "deploy", "--rehearse", str(state), env=env)
    assert result.returncode != 0
    lines = commands(tmp_path)
    assert any("stop scheduler api worker caddy monitor" in line for line in lines)
    assert not any("up -d --no-deps scheduler" in line for line in lines)


def test_private_rollback_snapshots_with_outgoing_image_before_compatibility(
    tmp_path: Path,
) -> None:
    env, state = rehearsal(
        tmp_path,
        PB_DEPLOY_TOPOLOGY="cloudrun-private-vm",
        PB_PUBLIC_ORIGIN="https://offline.example.invalid",
        PB_PRIVATE_VM_IP="10.40.0.2",
        PB_PRIVATE_API_HOST="api.private.invalid",
        PB_PRIVATE_TLS_PATH=str(tmp_path / "tls"),
    )
    old_lock = write_image_lock(tmp_path / "old/deploy/private-images.json", commit="2" * 40)
    (tmp_path / "data/parkingbreaker.sqlite").touch()
    result = run_script("lifecycle.sh", "rollback", "--rehearse", str(state), env=env)
    assert result.returncode == 0, result.stderr
    lines = commands(tmp_path)
    snapshot = index(lines, "pb-previous", str(old_lock), "recovery_cli backup")
    assert "PYTHONPATH=/ops" not in lines[snapshot]
    assert snapshot < index(
        lines, str(Path(env["PB_RELEASE"]) / "deploy/private-images.json"), "compat.sqlite"
    )


@pytest.mark.parametrize(
    ("probe", "payload"),
    [
        ("SHIM_PUBLIC_HEALTH", "<html>Sign in</html>"),
        ("SHIM_HTTP_STATUS", "302"),
        ("SHIM_PUBLIC_HEALTH", json.dumps({"status": "ok", "mode": "fake"})),
        (
            "SHIM_PUBLIC_HEALTH",
            json.dumps({"status": "ok", "mode": "live", "build_commit": "1" * 40}),
        ),
        (
            "SHIM_LOCAL_HEALTH",
            json.dumps({"status": "ok", "mode": "fake", "build_commit": "2" * 40}),
        ),
    ],
)
def test_private_release_health_requires_the_reviewed_build_and_mode(
    tmp_path: Path, probe: str, payload: str
) -> None:
    env, state = rehearsal(
        tmp_path,
        PB_DEPLOY_TOPOLOGY="cloudrun-private-vm",
        PB_PUBLIC_ORIGIN="https://offline.example.invalid",
        PB_PRIVATE_VM_IP="10.40.0.2",
        PB_PRIVATE_API_HOST="api.private.invalid",
        PB_PRIVATE_TLS_PATH=str(tmp_path / "tls"),
        **{probe: payload},
    )
    result = run_script("lifecycle.sh", "deploy", "--rehearse", str(state), env=env)
    assert result.returncode == 5
    assert (state / "PROCESSING_BLOCKED").exists()
    assert not any("up -d --no-deps scheduler" in line for line in commands(tmp_path))
    assert (state / "PROCESSING_BLOCKED").exists()


def test_unknown_topology_refuses_before_any_host_command(tmp_path: Path) -> None:
    env, state = rehearsal(tmp_path, PB_DEPLOY_TOPOLOGY="typo")
    result = run_script("lifecycle.sh", "deploy", "--rehearse", str(state), env=env)
    assert result.returncode == 2
    assert commands(tmp_path) == []


@pytest.mark.parametrize(
    "failure", ["mutable", "missing", "commit", "split_backend", "mount", "image_label", "pull"]
)
def test_invalid_private_release_preserves_running_stack(tmp_path: Path, failure: str) -> None:
    env, state = rehearsal(
        tmp_path,
        PB_DEPLOY_TOPOLOGY="cloudrun-private-vm",
        PB_PUBLIC_ORIGIN="https://offline.example.invalid",
        PB_PRIVATE_VM_IP="10.40.0.2",
        PB_PRIVATE_API_HOST="api.private.invalid",
        PB_PRIVATE_TLS_PATH=str(tmp_path / "tls"),
    )
    lock = Path(env["PB_RELEASE"]) / "deploy/private-images.json"
    data = json.loads(lock.read_text())
    if failure == "mutable":
        data["services"]["api"]["image"] = "registry.invalid/pb/backend:latest"
    elif failure == "commit":
        data["x-pb-build-commit"] = "2" * 40
    elif failure == "split_backend":
        data["services"]["worker"] = data["services"]["caddy"]
    elif failure == "mount":
        data["services"]["scheduler"]["volumes"] = ["/unexpected"]
    elif failure == "image_label":
        env["SHIM_IMAGE_COMMIT"] = "3" * 40
    elif failure == "pull":
        env["SHIM_FAIL_MATCH"] = "pull api worker monitor caddy scheduler"
    lock.write_text(json.dumps(data))
    if failure == "missing":
        lock.unlink()
    result = run_script("lifecycle.sh", "rollback", "--rehearse", str(state), env=env)
    assert result.returncode != 0
    assert not any("stop scheduler" in line or " down " in line for line in commands(tmp_path))
    assert not (state / "PROCESSING_BLOCKED").exists()


def test_private_topology_requires_the_same_reviewed_public_origin(tmp_path: Path) -> None:
    env, state = rehearsal(
        tmp_path,
        PB_DEPLOY_TOPOLOGY="cloudrun-private-vm",
        PB_PUBLIC_ORIGIN="https://different.example.invalid",
        PB_PRIVATE_VM_IP="10.40.0.2",
        PB_PRIVATE_API_HOST="api.private.invalid",
        PB_PRIVATE_TLS_PATH=str(tmp_path / "tls"),
    )
    result = run_script("lifecycle.sh", "deploy", "--rehearse", str(state), env=env)
    assert result.returncode == 2
    assert commands(tmp_path) == []


def test_cloudbuild_backend_requires_full_commit_revision_before_build() -> None:
    recipe = (ROOT / "cloudbuild-backend.yaml").read_text()
    dockerfile = (ROOT / "backend/Dockerfile").read_text()
    assert "^[0-9a-f]{40}$$" in recipe
    assert recipe.index("^[0-9a-f]{40}$$") < recipe.index("'build'")
    assert "'PB_BUILD_COMMIT=${_PB_BUILD_COMMIT}'" in recipe
    assert "LABEL org.opencontainers.image.revision=$PB_BUILD_COMMIT" in dockerfile


@pytest.mark.parametrize(
    ("commit", "tag", "expected"),
    [
        ("a" * 40, "reviewed-candidate", 0),
        ("", "reviewed-candidate", 1),
        ("a" * 39, "reviewed-candidate", 1),
        ("A" * 40, "reviewed-candidate", 1),
        ("a" * 40 + "\n", "reviewed-candidate", 1),
        ('quoted value " with punctuation', "reviewed-candidate", 1),
        ("a" * 40, "", 1),
    ],
)
def test_cloudbuild_backend_executes_guard_with_substitutions_as_data(
    commit: str, tag: str, expected: int
) -> None:
    recipe = (ROOT / "cloudbuild-backend.yaml").read_text()
    assert "'PB_EXPECTED_COMMIT=${_PB_BUILD_COMMIT}'" in recipe
    assert "'PB_BUILD_IMAGE_TAG=${_IMAGE_TAG}'" in recipe
    match = re.search(r"      - \|-\n((?: {8}[^\n]*\n)+)", recipe)
    assert match is not None
    guard = dedent(match[1])
    assert "${_PB_BUILD_COMMIT}" not in guard
    assert "${_IMAGE_TAG}" not in guard
    result = subprocess.run(
        ["sh", "-ec", guard.replace("$$", "$")],
        env={**os.environ, "PB_EXPECTED_COMMIT": commit, "PB_BUILD_IMAGE_TAG": tag},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected
