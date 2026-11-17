# ParkingBreaker

Canonical GitHub-backed codebase: Python 3.12/FastAPI backend and Next.js frontend.

## Start here

| Document | Authority |
| --- | --- |
| [Execution roadmap](docs/ROADMAP.md) | Remaining work, decisions, dependencies and acceptance criteria |
| [Beta readiness](docs/BETA-READINESS.md) | Current release decision and source-specific evidence |
| [Engineering workflow](AGENTS.md) | Worker selection, supervision and telemetry constraints |
| [Telemetry foundation](docs/TELEMETRY-FOUNDATION.md) | Implemented ledger schema, hooks and explicit limitations |
| [OCR routing](docs/OCR-ROUTING.md) | Provider selection, thresholds and customer confirmation |
| [Customer Choice Rules](docs/CUSTOMER-CHOICE-RULES.md) | Guarantee, causation and correction boundaries |
| [Controlled-beta operations](docs/operations/controlled-beta.md) / [service mode](docs/operations/service-mode.md) | Activation, stop and recovery procedures |
| [Consolidation record](docs/CONSOLIDATION.md) | Historical provenance and recovery paths; not a current task queue |

**Live beta remains NO-GO.** Implementation, tests, configuration, activation and measured results are separate states. No deployment, charging, mailing or ad spending is authorized merely by this checkout.

## Development

~~~sh
make sync                 # Python development dependencies via uv
make check                # backend lint, format, strict types and tests
make qualification-check  # offline research/image harness self-tests
python3 -m unittest discover -s tools/tests -v
cd web && npm ci           # frontend dependencies from lockfile
npm run dev               # http://localhost:3001
~~~

Use make web-check for frontend lint, types and production build. Configure local environment from .env.example; never commit credentials. PB_PROVIDER_MODE=preview or fake keeps development separate from live providers.

On Ubuntu, make sync selects /usr/bin/python3.12 and checks Linux OFD file-lock support. Override PYTHON only with a compatible Linux interpreter. Some packaged Python builds omit F_OFD_SETLK and cannot safely run the ownership locks.

## Repository boundaries

- backend and web contain the application and regression tests.
- deploy, compose.yaml and the Cloud Build manifests contain deployment/build inputs. Local compose uses fake providers; build manifests are not a complete production qualification.
- tools/qualification and build-kit/scripts provide qualification and browser rehearsal tooling.
- Hash-addressed build-kit/evidence/bench-pages captures and legacy reference contracts are test inputs. Their age does not make them disposable.
- docs/verification contains immutable historical evidence. It must not be presented as a fresh test of modified source.
- tools/deepseek-delegate is a retained optional helper, not the current default coding workflow. AGENTS.md defines the direct-coding default and delegation limits.
- Local dependency environments, generated builds, run logs, customer data, credentials and source exports stay out of Git. Preserve user editor settings.

Historical build-kit governance commands (doctor, status, next, accept) require archived local state and are not the active roadmap. Its run-check command works independently in this checkout.

Local browser campaigns use synthetic providers and temporary stores:

~~~sh
PB_TICKET_READING_PREVIEW=1 python3 build-kit/scripts/synthetic_browser_matrix.py --out /tmp/pb-free-UNIQUE
python3 build-kit/scripts/synthetic_rehearsal.py --service-phase paid --out /tmp/pb-paid-UNIQUE
~~~

Use a new output path per run. These checks do not qualify physical cameras, real providers, live mail or customer charges.
