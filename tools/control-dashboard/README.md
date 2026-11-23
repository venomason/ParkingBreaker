# ParkingBreaker Control Dashboard

Private, loopback-only (`127.0.0.1:3090`) operator console for ParkingBreaker.

## What it connects to
- `pb-api`: `/health`, `/auth/pricing`
- `pb-worker`: `/health`, `/internal/overseer/feed` (authenticated via `PB_OVERSEER_FEED_SECRET`, zero-PII T75 read-only telemetry)
- `pb-web`: `/`, `/terms`, `/refund-policy`, `/privacy`, `/robots.txt`, `/sitemap.xml`
- Repo verification receipts (`docs/verification/*.json`) and launch gate board (`~/Workspace/pb-coordination/GATE-STATUS.md`)

## Safety boundaries
- Refuses non-loopback bind hosts.
- Strictly read-only: never exposes customer PII and never exposes web-side write controls for live checkout or service mode (operator CLI commands are shown as copyable snippets per `docs/operations/service-mode.md`).
- Reads `PB_OVERSEER_FEED_SECRET` from env or `~/.pb-secrets/overseer-qual.secret` (mode 600) and never logs or renders the secret.

## Launch with IAP tunnel to `pb-qual-backend-01`
```sh
gcloud compute ssh pb-qual-backend-01 --zone=us-west1-a --tunnel-through-iap -- -N \
  -L 127.0.0.1:18000:127.0.0.1:8000 \
  -L 127.0.0.1:18001:127.0.0.1:8001 \
  -L 127.0.0.1:13000:127.0.0.1:3000 &

python3 tools/control-dashboard/server.py --port 3090
```
Then open `http://127.0.0.1:3090/`.
