#!/usr/bin/env bash
# Same backup CLI for scheduled/interactive runs. No implicit deletion/retention.
#   --dry-run            seeded synthetic backup/restore/reconciliation in a temp directory
#   --rehearse <dir>     the --apply command bodies without root, state in <dir>; tests put
#                        recording docker stand-ins first on PATH
#   --apply              owner-run as root on the target VM (also the systemd service)
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
mode=${1:---dry-run}
if [[ $mode == --dry-run ]]; then
  export PYTHONPATH="$here/../backend/src"
  # Use the checkout's installed runtime when available so offline rehearsals
  # work without requiring provider packages in the host interpreter.
  project_python="$here/../backend/.venv/bin/python"
  if [[ ! -x $project_python ]]; then project_python=python3; fi
  exec "${PB_PYTHON:-${project_python}}" -m parkingbreaker.recovery_cli backup --dry-run
fi
if [[ $mode == --rehearse ]]; then
  state=${2:?isolated temp directory}
elif [[ $mode == --apply && $EUID == 0 ]]; then
  state=/run/parkingbreaker
else
  echo 'owner must run as root with --apply' >&2
  exit 2
fi
: "${PB_RELEASE:?}" "${PB_ENV_FILE:?}" "${PB_DATA_PATH:?}" "${PB_TLS_PATH:?}" "${PB_DOMAIN:?}"
umask 077
mkdir -p "$state"
exec 9>"$state/lifecycle.lock"
flock -n 9 || { echo '{"event":"backup_skipped_lifecycle_active"}' >&2; exit 3; }
failed() {
  result=$?
  if (( result != 0 )); then
    printf '%s\n' '{"event":"backup_job_failed"}' >&2
    owner=()
    if (( EUID == 0 )); then owner=(-o 10001 -g 10001); fi
    install -d -m 0700 "${owner[@]}" "$PB_DATA_PATH/recovery"
    # Append only: earlier alerts and every earlier bundle stay exactly as they were.
    printf '%s\n' '{"event":"backup_job_failed"}' >> "$PB_DATA_PATH/recovery/recovery-alerts.jsonl"
    if (( EUID == 0 )); then chown 10001:10001 "$PB_DATA_PATH/recovery/recovery-alerts.jsonl"; fi
  fi
}
trap failed EXIT
compose() { docker compose --env-file "$PB_ENV_FILE" -p parkingbreaker -f "$PB_RELEASE/compose.yaml" -f "$PB_RELEASE/deploy/compose.production.yaml" "$@"; }
name="scheduled-$(date -u +%Y%m%dT%H%M%SZ)-$$"
compose exec -T api python -m parkingbreaker.recovery_cli backup --data /data --output "/data/recovery/$name"
if [[ -n ${PB_REPLICA_DIRECTORY:-} ]]; then
  # Operator-mounted independent off-site transport: never a SQLite authority mount.
  [[ -d $PB_REPLICA_DIRECTORY && ! -L $PB_REPLICA_DIRECTORY ]]
  compose run --rm --no-deps -v "$PB_REPLICA_DIRECTORY:/replica" api python -m parkingbreaker.recovery_cli replicate --data "/data/recovery/$name" --output "/replica/$name"
fi
