#!/bin/sh
# Call the worker sweep every minute (T24). A failed or hung call is logged with its curl
# exit code and HTTP status, never hidden, and the loop keeps going. The response body is
# discarded. Health comes from the recorded sweep runs, not from this loop surviving.
# Usage: scheduler-loop.sh <dispatch URL> [follow-up URL].
# PB_SCHEDULER_CALLS=N stops after N calls (tests).
set -u
url="$1"
follow_url="${2:-}"
interval="${PB_SCHEDULER_INTERVAL:-60}"
calls=0
while :; do
  calls=$((calls + 1))
  status=$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 300 \
    -X POST -H "Authorization: Bearer ${PB_WORKER_SECRET}" "$url")
  code=$?
  if [ "$code" -ne 0 ] || [ "$status" != 200 ]; then
    printf '{"event":"sweep_call_failed","curl_exit":%s,"http_status":"%s"}\n' "$code" "${status:-000}" >&2
  else
    printf '{"event":"sweep_call_ok"}\n'
  fi
  if [ -n "$follow_url" ]; then
    # The follow-up sweep is independent: its failure never blocks the mailing sweep.
    fstatus=$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 60 \
      -X POST -H "Authorization: Bearer ${PB_WORKER_SECRET}" "$follow_url")
    if [ "$fstatus" != 200 ]; then
      printf '{"event":"follow_up_call_failed","http_status":"%s"}\n' "${fstatus:-000}" >&2
    fi
  fi
  if [ -n "${PB_SCHEDULER_CALLS:-}" ] && [ "$calls" -ge "$PB_SCHEDULER_CALLS" ]; then
    exit 0
  fi
  sleep "$interval"
done
