"""Regional disk identity and secret permissions are passed into pure deployment policy."""

import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from parkingbreaker.deployment_policy import protected_env, regional_owner

ROOT = Path(__file__).resolve().parents[2]

VM = "https://compute.googleapis.com/compute/v1/projects/example/zones/us-west1-a/instances/pb"
DISK: dict[str, object] = {
    "name": "pb-data",
    "region": "/projects/example/regions/us-west1",
    "replicaZones": ["/zones/us-west1-a", "/zones/us-west1-b"],
    "users": [VM],
    "accessMode": "READ_WRITE_SINGLE",
    "status": "READY",
}


def test_regional_disk_must_be_this_attached_device_and_only_this_vm() -> None:
    assert regional_owner(DISK, VM, "/dev/disk/by-id/google-pb-data")
    assert not regional_owner(DISK, VM, "/dev/sda1")
    for field, value in [
        ("users", [VM, "another-host"]),
        ("users", []),
        ("replicaZones", ["/zones/us-west1-a"]),
        ("accessMode", "READ_WRITE_MANY"),
        ("region", "/zones/us-west1-a"),
        ("status", "CREATING"),
    ]:
        changed = deepcopy(DISK)
        changed[field] = value
        assert not regional_owner(changed, VM, "/dev/disk/by-id/google-pb-data")


@pytest.mark.parametrize(
    ("uid", "mode", "regular"), [(1000, 0o600, True), (0, 0o644, True), (0, 0o600, False)]
)
def test_secret_rejects_wrong_owner_permissions_or_symlink(
    uid: int, mode: int, regular: bool
) -> None:
    assert protected_env(0, 0o600, True)
    assert not protected_env(uid, mode, regular)


# ---- validate.sh target mode: the shell glue around the pure policy, exercised offline ----


def _validate(
    tmp_path: Path, env_file: Path, disk: dict[str, object]
) -> "subprocess.CompletedProcess[str]":
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "disk.json").write_text(json.dumps(disk))
    return subprocess.run(
        ["bash", str(ROOT / "deploy/validate.sh")],
        capture_output=True,
        text=True,
        check=False,
        env=dict(
            os.environ,
            PB_ENV_FILE=str(env_file),
            PB_DATA_PATH=str(tmp_path / "data"),
            PB_TLS_PATH=str(tmp_path / "tls"),
            PB_DISK_MOUNT=str(tmp_path / "mount"),
            PB_DISK_DEVICE="/dev/disk/by-id/google-pb-data",
            PB_DISK_JSON=str(tmp_path / "disk.json"),
            PB_VM_ID=VM,
        ),
    )


def test_validate_refuses_an_env_file_that_is_not_root_owned_0600(tmp_path: Path) -> None:
    env_file = tmp_path / "app.env"
    env_file.write_text("PB_WORKER_SECRET=synthetic\n")
    env_file.chmod(0o600)
    result = _validate(tmp_path, env_file, DISK)
    assert result.returncode != 0
    assert "root-owned regular 0600" in result.stderr
    assert "synthetic" not in result.stdout + result.stderr  # secret content never echoed


def test_validate_refuses_a_disk_with_another_user_even_for_a_protected_env(
    tmp_path: Path,
) -> None:
    # lstat()s only: any root-owned 0600 regular file stands in for the protected env file.
    candidates = [
        path
        for path in (Path("/etc/.pwd.lock"), *sorted(Path("/etc").glob("netplan/*.yaml")))
        if path.is_file()
        and not path.is_symlink()
        and path.lstat().st_uid == 0
        and path.lstat().st_mode & 0o777 == 0o600
    ]
    if not candidates:
        pytest.skip("no root-owned 0600 stand-in file on this host")
    changed = dict(DISK, users=[VM, "another-host"])
    result = _validate(tmp_path, candidates[0], changed)
    assert result.returncode != 0
    assert "exactly this VM" in result.stderr
