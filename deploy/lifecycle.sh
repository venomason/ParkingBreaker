#!/usr/bin/env bash
# One stop-before-start path for deploy and rollback. Root/env access is owner-run.
#   --dry-run            print the plan only (default)
#   --rehearse <dir>     run the same command bodies as --apply with state in <dir>, without
#                        root; tests put recording docker/gcloud/curl stand-ins first on PATH.
#                        Only validate.sh (root-owned env, mounted regional device) is skipped.
#   --apply              owner-run as root on the target VM
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
action=${1:?deploy or rollback}; shift
[[ $action == deploy || $action == rollback ]] || exit 2
mode=${1:---dry-run}
topology=${PB_DEPLOY_TOPOLOGY:-single-vm}
old_topology=${PB_OLD_DEPLOY_TOPOLOGY:-$topology}
for selected in "$topology" "$old_topology"; do
  [[ $selected == single-vm || $selected == cloudrun-private-vm ]] || {
    echo 'unsupported deployment topology' >&2; exit 2;
  }
done
if [[ $mode == --dry-run ]]; then
  printf '%s\n' "topology=$topology; previous_topology=$old_topology"
  printf '%s\n' "OWNER PLAN $action: lock; validate fresh regional disk; prepare images (private VM: pull reviewed digests and check commit); refuse quarantined restore; stop scheduler; down old; verify no prior owners; snapshot; migration/compatibility; start services; health; start scheduler; fresh monitor poll and recorded ok sweep (180s maximum wait)"
  exit 0
fi
rehearsal=
candidate_started=0
if [[ $mode == --rehearse ]]; then
  rehearsal=${2:?isolated temp directory}
  state=$rehearsal
elif [[ $mode == --apply && $EUID == 0 ]]; then
  state=/run/parkingbreaker
else
  echo 'owner must run as root with --apply' >&2
  exit 2
fi
# Everything the later steps need is checked before anything is stopped.
: "${PB_RELEASE:?immutable release directory}" "${PB_OLD_RELEASE:?}" "${PB_OLD_PROJECT:?explicit old compose project}" "${PB_ENV_FILE:?}" "${PB_DATA_PATH:?}" "${PB_TLS_PATH:?}" "${PB_DOMAIN:?public DNS name}" "${PB_DISK_NAME:?}" "${PB_VM_ID:?}" "${PB_DISK_MOUNT:?}" "${PB_DISK_DEVICE:?}"
if [[ $topology == cloudrun-private-vm ]]; then
  : "${PB_PUBLIC_ORIGIN:?}" "${PB_PRIVATE_VM_IP:?}" "${PB_PRIVATE_API_HOST:?}" "${PB_PRIVATE_TLS_PATH:?}"
  [[ ${PB_BUILD_COMMIT:-} =~ ^[0-9a-f]{40}$ && ${PB_PROVIDER_MODE:-} =~ ^(preview|fake|test|live)$ ]] || {
    echo 'private cutover requires the reviewed full build commit and provider mode' >&2; exit 2;
  }
  [[ $PB_PUBLIC_ORIGIN == "https://$PB_DOMAIN" ]] || {
    echo 'public origin must match the reviewed customer domain' >&2; exit 2;
  }
fi
compose_files=(-f "$PB_RELEASE/compose.yaml" -f "$PB_RELEASE/deploy/compose.production.yaml")
old_compose_files=(-f "$PB_OLD_RELEASE/compose.yaml" -f "$PB_OLD_RELEASE/deploy/compose.production.yaml")
build_services=(api worker web monitor)
runtime_services=(api worker web caddy monitor)
start_step='start api web caddy'
if [[ $topology == cloudrun-private-vm ]]; then
  image_lock="$PB_RELEASE/deploy/private-images.json"
  backend_image=$(python3 "$here/private-images.py" "$image_lock" "$PB_BUILD_COMMIT")
  compose_files+=(-f "$PB_RELEASE/deploy/compose.private-vm.yaml" -f "$image_lock")
  runtime_services=(api worker caddy monitor)
  start_step='start api private caddy'
fi
if [[ $old_topology == cloudrun-private-vm ]]; then
  old_compose_files+=(-f "$PB_OLD_RELEASE/deploy/compose.private-vm.yaml")
  old_lock="$PB_OLD_RELEASE/deploy/private-images.json"
  if [[ -e $old_lock ]]; then
    python3 "$here/private-images.py" "$old_lock" >/dev/null
    old_compose_files+=(-f "$old_lock")
  fi
fi
mkdir -p "$state"
exec 9>"$state/lifecycle.lock"
flock -n 9 || { echo 'another lifecycle owns this host' >&2; exit 3; }
compose() { docker compose --env-file "$PB_ENV_FILE" -p parkingbreaker "${compose_files[@]}" "$@"; }
old_compose() { docker compose --env-file "$PB_ENV_FILE" -p "$PB_OLD_PROJECT" "${old_compose_files[@]}" "$@"; }
readiness_compose() {
  local remaining=$(( readiness_deadline - SECONDS ))
  (( remaining > 0 )) || return 1
  (( remaining <= 5 )) || remaining=5
  timeout "${remaining}s" docker compose --env-file "$PB_ENV_FILE" -p parkingbreaker "${compose_files[@]}" "$@"
}
matches_release_health() {
  python3 -c '
import json, sys
try:
    raw = sys.stdin.buffer.read(8193)
    if len(raw) > 8192:
        sys.exit(1)
    body, status = raw.rsplit(b"\n", 1)
    if status != b"200":
        sys.exit(1)
    payload = json.loads(body)
except (ValueError, RecursionError):
    sys.exit(1)
sys.exit(0 if isinstance(payload, dict) and payload.get("status") == "ok"
         and payload.get("build_commit") == sys.argv[1] and payload.get("mode") == sys.argv[2] else 1)
' "$PB_BUILD_COMMIT" "$PB_PROVIDER_MODE"
}
perform() {
  local step=$1
  printf '%s\n' "$step" >> "$state/events"
  case "$step" in
    validate)
      PB_DISK_JSON="$state/disk.json"; export PB_DISK_JSON
      gcloud compute disks describe "$PB_DISK_NAME" --region=us-west1 --format=json > "$PB_DISK_JSON"
      [[ -n $rehearsal ]] || PB_DEPLOY_TOPOLOGY="$topology" bash "$here/validate.sh"
      compose config --quiet
      old_compose config --quiet
      if [[ $topology == cloudrun-private-vm ]]; then
        compose pull api worker monitor caddy scheduler
        image_commit=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$backend_image")
        [[ $image_commit == "$PB_BUILD_COMMIT" ]] || { echo 'backend image revision mismatch' >&2; return 1; }
      else
        compose build "${build_services[@]}"
      fi ;;
    'quarantine gate')
      # Restored, unreconciled data must never be migrated, opened or served.
      if [[ -e $PB_DATA_PATH/RESTORE_QUARANTINE.json ]]; then
        echo 'restored data remains quarantined; original-provider reconciliation required' >&2
        return 4
      fi ;;
    'stop scheduler') old_compose stop scheduler ;;
    'down old') old_compose down --remove-orphans ;;
    'assert exclusive')
      local role owners
      for role in api worker scheduler; do
        owners=$(docker ps -q --filter "label=com.docker.compose.service=$role") || return 1
        [[ -z $owners ]] || return 1
      done ;;
    snapshot)
      if [[ ! -f $PB_DATA_PATH/parkingbreaker.sqlite ]]; then
        [[ $action == deploy ]] || return 1
        printf '%s\n' 'first deployment: no prior database to snapshot'
        return 0
      fi
      # Recovery CLI uses SQLite backup, including WAL and all photo/PDF bytes.
      local -a backup_command=(compose run --rm --no-deps)
      if [[ $topology == single-vm ]]; then
        backup_command+=(-v "$here/../backend/src:/ops:ro" -e PYTHONPATH=/ops)
      elif [[ $old_topology == cloudrun-private-vm && -f $old_lock ]]; then
        # Snapshot using the outgoing release's recovery code before crossing schemas.
        backup_command=(old_compose run --rm --no-deps)
      fi
      "${backup_command[@]}" api python -m parkingbreaker.recovery_cli backup --data /data --output "/data/recovery/pre-$action-$(date -u +%Y%m%dT%H%M%SZ)" ;;
    migrate)
      compose run --rm --no-deps api python -c 'from pathlib import Path; from parkingbreaker.transaction_store import SqliteTransactionStore; s=SqliteTransactionStore(Path("/data/parkingbreaker.sqlite")); s.close()' ;;
    'compatibility check')
      # Never run old migrations against authority. Prove unchanged schema/data on a copy.
      compose run --rm --no-deps api python -c 'import sqlite3; from pathlib import Path; from parkingbreaker.transaction_store import SqliteTransactionStore; a=sqlite3.connect("file:/data/parkingbreaker.sqlite?mode=ro", uri=True); b=sqlite3.connect("/tmp/compat.sqlite"); a.backup(b); before=list(b.iterdump()); b.close(); s=SqliteTransactionStore(Path("/tmp/compat.sqlite")); s.close(); b=sqlite3.connect("/tmp/compat.sqlite"); assert before == list(b.iterdump()), "old release changes schema/data; roll forward required"' ;;
    'start api web caddy'|'start api private caddy')
      compose up -d --no-deps "${runtime_services[@]}" ;;
    health)
      local -a public_health=(curl --fail --silent --show-error --max-time 5 "https://$PB_DOMAIN/api/health")
      if [[ $topology == single-vm ]]; then
        compose exec -T api python -c 'import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2).status == 200' || return 1
        compose exec -T web node -e 'fetch("http://127.0.0.1:3000").then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))' || return 1
        "${public_health[@]}" >/dev/null || return 1
      else
        compose exec -T api python -c 'import http.client, sys
c = http.client.HTTPConnection("127.0.0.1", 8000, timeout=2)
c.request("GET", "/health")
r = c.getresponse()
sys.stdout.buffer.write(r.read(8193) + f"\n{r.status}".encode())' | matches_release_health || return 1
        "${public_health[@]}" --write-out '\n%{http_code}' | matches_release_health || return 1
      fi
      compose exec -T monitor python -c 'import parkingbreaker.health_cli' ;;
    'stop candidate') compose stop scheduler "${runtime_services[@]}" ;;
    'start scheduler')
      compose up -d --no-deps scheduler &&
      compose exec -T scheduler test -r /pb/scheduler-loop.sh ;;
    readiness)
      # Exec requires a live container. The validator also checks the actual monitor
      # process identity, its completed observation and the read-only sweep record.
      readiness_compose exec -T scheduler test -r /pb/scheduler-loop.sh &&
      readiness_compose exec -T monitor python -m parkingbreaker.release_readiness --data /data --state /monitor --not-before "$candidate_started_at" ;;
  esac
}
cleanup() {
  result=$?
  if (( result != 0 && candidate_started == 1 )); then
    perform 'stop candidate' || true
    touch "$state/PROCESSING_BLOCKED"
  fi
}
trap cleanup EXIT
perform validate
perform 'quarantine gate'
perform 'stop scheduler'
perform 'down old'
touch "$state/PROCESSING_BLOCKED"
perform 'assert exclusive'
perform snapshot
if [[ $action == deploy ]]; then perform migrate; else perform 'compatibility check'; fi
candidate_started=1
candidate_started_at=$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ)
perform "$start_step"
healthy=0
for (( attempt=0; attempt<30; attempt++ )); do
  if perform health; then healthy=1; break; fi
  if [[ -z $rehearsal ]]; then sleep 1; fi
done
(( healthy == 1 )) || exit 5
perform 'start scheduler'
ready=0
readiness_deadline=$(( SECONDS + 180 ))
# At most 90 polls, 2s apart, within a 180s deadline. Each Docker exec is bounded
# by the remaining deadline and a 5s command cap. Rehearsals omit only the sleeps.
for (( attempt=0; attempt<90 && SECONDS<readiness_deadline; attempt++ )); do
  if perform readiness; then ready=1; break; fi
  if [[ -z $rehearsal ]]; then
    remaining=$(( readiness_deadline - SECONDS ))
    if (( remaining > 0 )); then
      (( remaining <= 2 )) || remaining=2
      sleep "$remaining"
    fi
  fi
done
(( ready == 1 )) || exit 6
rm "$state/PROCESSING_BLOCKED"
