# ParkingBreaker

Codebase-only branch for GitHub. Python 3.12/FastAPI backend and Next.js frontend.
This branch starts with fresh history: operational journals, review archives, exports,
logs, caches, installed dependencies and prior Git history are excluded.

## Development

```sh
make sync             # Python development dependencies via uv
make check            # backend lint, format, strict types and tests
cd web && npm ci      # frontend dependencies from lockfile
npm run dev           # http://localhost:3001
```

Use `make web-check` for lint, types and production build. Configure local environment
from `.env.example`; never commit credentials. `PB_PROVIDER_MODE=preview` or `fake`
keeps development separate from live providers. Existing test fixtures and runtime
free-beta policy are retained, including the official-page captures required by tests.
A few reference contracts retain their original paths because tests read them.

The original local branch preserves project governance and qualification evidence.
This branch does not establish launch readiness; unresolved source/paid-browser gates
remain open. No deployment, charging or mailing is authorized by this checkout.

`tools/deepseek-delegate` contains the bounded engineering helper, without run receipts.
From that tool directory pass `--source ../..` for this application root. Browser rehearsal
scripts remain source tooling; historical run records are intentionally absent.
