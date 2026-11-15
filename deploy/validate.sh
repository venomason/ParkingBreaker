#!/usr/bin/env bash
# Owner-run read-only checks. No cloud resource creation.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ ${1:-} == --dry-run ]]; then
  bash -n "$here/"*.sh
  if command -v shellcheck >/dev/null; then shellcheck "$here/"*.sh; fi
  printf '%s\n' 'offline syntax checks passed; target validation requires env, mounted regional disk and fresh disk describe'
  exit 0
fi
: "${PB_DISK_MOUNT:?}" "${PB_DISK_DEVICE:?}" "${PB_ENV_FILE:?}" "${PB_DATA_PATH:?}" "${PB_TLS_PATH:?}" "${PB_DISK_JSON:?}" "${PB_VM_ID:?}"
PYTHONPATH="$here/../backend/src" python3 - "$PB_ENV_FILE" "$PB_DATA_PATH" "$PB_DISK_JSON" "$PB_VM_ID" "$PB_DISK_DEVICE" <<'PY'
import json, pathlib, stat, sys
from parkingbreaker.deployment_policy import protected_env, regional_owner
secret, data, evidence = map(pathlib.Path, sys.argv[1:4])
s = secret.lstat()
assert protected_env(s.st_uid, stat.S_IMODE(s.st_mode), stat.S_ISREG(s.st_mode)), 'env must be root-owned regular 0600'
assert data.is_dir() and not data.is_symlink(), 'data directory required'
disk = json.loads(evidence.read_text())
assert regional_owner(disk, sys.argv[4], sys.argv[5]), 'regional attached device must have exactly this VM as single writer'
# No secret content is printed or executed as shell code.
PY
findmnt --mountpoint "$PB_DISK_MOUNT" --source "$PB_DISK_DEVICE" --options rw >/dev/null
[[ $(readlink -f "$PB_DATA_PATH") == "$(readlink -f "$PB_DISK_MOUNT")/data" ]]
[[ $(findmnt -n -o FSTYPE --mountpoint "$PB_DISK_MOUNT") == ext4 ]]
release=${PB_RELEASE:-$here/..}
files=(-f "$release/compose.yaml" -f "$release/deploy/compose.production.yaml")
case ${PB_DEPLOY_TOPOLOGY:-single-vm} in
  single-vm) ;;
  cloudrun-private-vm)
    : "${PB_PUBLIC_ORIGIN:?}" "${PB_PRIVATE_VM_IP:?}" "${PB_PRIVATE_API_HOST:?}" "${PB_PRIVATE_TLS_PATH:?}"
    files+=(-f "$release/deploy/compose.private-vm.yaml") ;;
  *) echo 'unsupported deployment topology' >&2; exit 2 ;;
esac
docker compose --env-file "$PB_ENV_FILE" -p parkingbreaker "${files[@]}" config --quiet
printf '%s\n' 'target env, regional single-owner disk, mount and compose validated'
