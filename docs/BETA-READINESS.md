# Controlled-beta readiness

Updated November 17, 2026 (Pacific). **NO-GO for live paid beta.**

**Preview deployed:** [parkingbreaker.com](https://parkingbreaker.com) serves application commit `0bf011b7c243bed1b864bdb1739438469fdacb58` on `parkingbreaker-frontend-preview-0bf011b`. Both container digests, 100% traffic, page/assets and ticket-upload/synthetic-OCR checks are recorded in [the deployment receipt](verification/deployment-preview-2026-11-17.json). It uses fake providers, the paid product flow and temporary preview storage; no real payments or mailing were activated.

The [execution roadmap](ROADMAP.md) owns remaining tasks and acceptance criteria. This page owns the evidence summary and release decision. The [controlled-beta operating packet](operations/controlled-beta.md) owns the activation/recovery procedure. No source export, test run, GitHub publication or roadmap is itself deployment authority.

## Current source status

The November 17 publication candidate extends commit a717d5ab08dbed349406e0171bc29eddec3419c8 with journey-ledger, OCR/manual-review observability, T01 reconstruction, mandatory OCR and documentation work. Its exact source manifest and checks are in [the publication receipt](verification/publication-2026-11-17.json). The ledger, OCR/manual-review slice, T01 read-only reconstruction and required OCR-attempt gate are implemented and locally tested; see [Telemetry Foundation](TELEMETRY-FOUNDATION.md) for scope, atomicity, replay guarantees and limitations. No persistent analyzer, exporter or autonomous repair is live. Owner direction is paid beta: the example uses fake providers with the paid flow. Launch requires both BETA and PAID admission gates; no free entitlement batch is planned.

The product remains clerical. Essential unknown facts block charge/dispatch; the customer supplies and confirms their own facts and reasoning. Do not infer legal conclusions, silently substitute providers, reopen retired unverified-address overrides or create a city-by-city launch allowlist.

OCR selection is configurable. [OCR routing](OCR-ROUTING.md) documents the primary and optional secondary routes; a selectable model does not establish that it is configured or qualified in production. [Customer Choice Rules](CUSTOMER-CHOICE-RULES.md) owns guarantee/causation semantics.

## Evidence and its limits

| Evidence | What it supports | What it does not establish |
| --- | --- | --- |
| [November 17 publication candidate](verification/publication-2026-11-17.json) | 13,754 backend tests across the full disk-backed run and separate short-path socket check; strict types/lint/format, frontend build/lint/types, 152 paid browser checks, 53 OCR mobile checks and 3 telemetry tests | Preview later deployed under its separate receipt; no paid activation or live-provider qualification |
| [November 16 publication receipt](verification/publication-2026-11-16.json) | 13,643 backend tests, global Ruff lint/format, strict source types and offline qualification self-tests on the recorded input digest | Full regression of subsequent journey changes; current deployed health |
| [Later polish verification](verification/polish-2026-11-16.json) | 13,690 backend tests, 152 paid browser checks, 50 beta mobile-viewport checks, lint/types/build on its recorded fingerprint | Physical-device qualification, live providers, or a full run after the journey-ledger slice |
| [Browser rehearsal index](verification/browser-rehearsals-2026-11-16.json) | Two passing historical rehearsals (49 and 50 checks), plus preserved interrupted/failed attempts and recovery hashes | A claim that every attempt passed or that those fingerprints equal current source |
| [November 17 OCR/manual-review acceptance](verification/ocr-manual-review-2026-11-17.json) | 13,722 backend tests, lint/format/types, frontend checks/build and 53 synthetic mobile browser checks; frozen-commitment invariance and pinned guidance rollback verified | Production activation, real provider/device qualification, complete journey reconstruction or autonomous repair |
| Journey foundation acceptance | 946 existing checks across 34 relevant modules plus 10 new journey tests; targeted lint/types | Complete journey capture, external analytics or production activation |
| [Cleanup follow-up](verification/repo-cleanup-2026-11-16.json) | 10 journey tests, 43 retained-fixture/contract tests and 3 tooling tests; lint/format/strict mypy on seven touched files; local documentation links checked | A new full-suite release qualification |
| [November 17 journey reconstruction and OCR gate](verification/journey-reconstruction-2026-11-17.json) | Read-only timeline, route-aware gap classification, snapshot pagination, frozen-state isolation and mandatory current-photo OCR attempt tests; backend/frontend lint and types | Full attempt history, live provider delivery or paid-beta activation |
| [Android smoke receipt](verification/android-smoke-2026-11-15.json) and retained local phone receipts | Source-specific actual-device/fake-provider flows | Current iPhone/Safari, landscape, production TLS or live OCR |
| [Corrected live reader assessment](verification/deepseek-corrected-live-2026-11-16.json) and [earlier checkpoint](verification/deepseek-first-live-2026-11-15.json) | Bounded real-provider component evidence and known failures | Newest prompt/provider combination or entire live customer path |

Historical publication input digest: 20eed44a059e83b616bb741741c669f5e4cb08c462992fe3318b89c0e9cd8df4. Do not relabel it as the current candidate. Exact commands and fingerprints belong in their receipts. New release acceptance must identify its own source, configuration and immutable artifact.

### Preserved provider findings

Two completed DeepSeek trial envelopes made 15 unique calls. Reservations totaled $0.51; the conservative upward-rounded application usage estimate was $0.15. These are historical budget figures, not provider invoices or ongoing spending authority. Brave runtime access remains unqualified.

A/B/E extraction passed in the retained assessment; C was refused with original cause unknown; D was refused for a multiline issuer and also exposed an O/0 key ambiguity; F correctly left the citation unreadable. Additional comparison probes were component diagnostics. In production, TicketFactBinding.review_fields excludes adopted-only/null literal fields from comparison; no wrong fact was demonstrated to be automatically adopted into a production case. Do not force a fixture's ambiguous character or log raw customer OCR to make a test pass.

Measure field accuracy, safe refusal, customer-correction completion and fulfillment eligibility separately. New prompts and the current complete coordinator path still need a finite production-aligned qualification. An unused or unapproved probe plan is not authorization.

### Preserved device findings

Historical A56/Android fake-provider tests exercised front/back file selection, cancel, missing-fact blocking, confirmation/reload, edit invalidation, bad-file refusal, injected connection failure, background/return, actual camera and native gallery. The camera image was fictional; stored pixels and dimensions survived the metadata-stripping filter. Raw-byte identity was not claimed.

Those results used older recorded fingerprints and a local transport bridge. Actual iPhone/Safari and landscape remain open. Reinspect devices, processes and ports; obsolete handoff PIDs are not current runtime facts.

## Release gates

Task IDs resolve to the authoritative roadmap.

| Gate | Current state | Next evidence |
| --- | --- | --- |
| Current candidate / CI | November 17 full backend suite and synthetic frontend rehearsal passed on recorded application inputs | R01: qualify remaining CI/release checks, build configuration and immutable candidate artifacts |
| OCR / research | Component evidence exists; latest end-to-end path unqualified | P01/P04: production-aligned manual/correction/refusal/provider trials with actual access and finite budget |
| Official evidence | 23 qualification rows retain uncertainties; 109 saved quotes historically checked | P04: resolve relevant procedure/evidence gaps; no fabricated source facts |
| Physical devices / access | Android evidence is source-specific; remaining device/email gates open | P02/P05: current iPhone, landscape, access-recovery and real delivery receipts |
| Target / security / recovery | Local contracts exist; named target acceptance missing | R02–R04: cost/IAM/TLS/storage/fencing, off-host restore, independent monitoring and all-intake stop |
| Printing / webhooks / money | Local controls exist; actual provider proof incomplete | P06: correct test accounts, PDF inspection, webhook/replay/expiry proof; live actions separately scoped |
| Outcomes / support | Local mechanisms exist; coverage and real operation unqualified | J05/P07: all-appeal follow-up, primary/backup support, real alert receipt and acknowledgment |
| Privacy / telemetry | Journey ledger is restricted; full coverage/retention/export policies open | J01/J06/R05: coverage reconciliation and governed data lifecycle |
| Activation | NO-GO | R06/R07: exact target, artifact, dates, funded cap, stop rules and enforced paid-cohort cap |
| Paid beta | Required launch offering; not activated | R07: paid gates, configured price/disclosures/reserves and bounded paid cohort; R08: reconcile actual costs/remedies |
| Unattended improvement | Ledger, OCR/manual-review observations and T01 reconstruction tested; queue not implemented | T01–T07 local proof, then T08/R09 for qualified production release |

Historical cloud inventories do not establish current service health. No new deployment, charge, mail, outreach, ad spend or provider trial is authorized by this page. Consolidation and recovery provenance remain in [CONSOLIDATION.md](CONSOLIDATION.md).
