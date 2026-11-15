# ParkingBreaker

Canonical GitHub-backed codebase. Python 3.12/FastAPI backend and Next.js frontend.
The November 15 consolidation imports the later application work from source commit
`996f35ee8839dc104ada29be5f9d0b2c806dc03e`, with provenance in
[the consolidation record](docs/CONSOLIDATION.md). Original Git history, operational
journals, review archives, exports, logs and local data remain preserved locally.

## Development

```sh
make sync             # Python development dependencies via uv
make check            # backend lint, format, strict types and tests
make qualification-check # offline research/image harness self-tests; no providers
python3 -m unittest discover -s tools/tests -v # clean-checkout tooling regressions
cd web && npm ci      # frontend dependencies from lockfile
npm run dev           # http://localhost:3001
```

Use `make web-check` for lint, types and production build. Configure local environment
from `.env.example`; never commit credentials. `PB_PROVIDER_MODE=preview` or `fake`
keeps development separate from live providers. Existing test fixtures and runtime
free-beta policy are retained, including the official-page captures required by tests.
A few reference contracts retain their original paths because tests read them.

On Ubuntu, `make sync` selects `/usr/bin/python3.12` and checks the required Linux
OFD file-lock support. Override with `make PYTHON=/path/to/python3.12 sync` only for
a compatible Linux interpreter. Some packaged Python builds omit `F_OFD_SETLK`;
those cannot safely run the existing ownership locks.

The original local branch preserves project governance and qualification evidence.
See [beta readiness](docs/BETA-READINESS.md) for verified local checks and remaining
release gates. No deployment, charging or mailing is authorized by this checkout.

`tools/deepseek-delegate` contains the bounded engineering helper, without run receipts.
From that tool directory pass `--source ../..` for this application root. Browser rehearsal
scripts remain source tooling; historical run records are intentionally absent.

Local browser campaigns use synthetic providers and temporary stores:

```sh
PB_TICKET_READING_PREVIEW=1 python3 build-kit/scripts/synthetic_browser_matrix.py --out /tmp/pb-free-UNIQUE
python3 build-kit/scripts/synthetic_rehearsal.py --service-phase paid --out /tmp/pb-paid-UNIQUE
```

Use a new output path for each run. These checks do not qualify physical cameras,
real providers, live mail or customer charges. Historical kit governance commands
(`doctor`, `status`, `next`, `accept`) still require their archived local state;
`run-check` works independently in this code-only checkout.
