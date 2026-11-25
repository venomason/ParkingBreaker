# Controlled-beta readiness

Updated November 25, 2026 (Pacific). **NO-GO for live paid beta.**
Paid activation requires the R07 owner packet; the roadmap gates own it.

The November 23 [ordinary First-Class feedback pilot](operations/controlled-beta.md#ordinary-first-class-feedback-pilot)
is in local qualification. Opening enrollment requires current official evidence for one
issuer and filing stage, plus a rehearsal on the intended deployment. No live pilot has
been opened as part of this work.

The [customer journey test](../backend/tests/test_first_class_pilot_journey.py) exercises a
$0 acceptance, packet approval, a restart and one ordinary-mail request to a fake provider.
Revoking the route after approval produces zero provider creates. The
[feedback tests](../backend/tests/test_pilot_feedback.py) cover reminder timing, opt-out,
enrollment, and separate customer reports about routing, acknowledgment and disposition.

**Preview deployed:** [parkingbreaker.com](https://parkingbreaker.com) serves application source `ffd1e848b9ed1feea3911f52075ca12743b38937` (code-identical to `main@d139c39`, verified by recursive diff) on revision `parkingbreaker-frontend-00012-tk6`, 100% traffic. Both container images are pinned by immutable digest (backend `b379bba0…`, frontend `b572e4d6…`). It uses fake providers, the paid product flow and temporary in-memory storage; no real payments or mailing are activated. Backend provider/consumer secrets now resolve from Secret Manager, so no plaintext consumer secret remains in the deployed environment. Evidence: the host-local deployment receipt (`~/Workspace/pb-r01/deploy-20261121T070749Z/receipt.json`) and [increment receipt](verification/increment-2026-11-21a.json).

The [appeal-rules verification](verification/filing-rules-2026-11-18.json) covers the stage-specific rules view, removal of ordinance lookup, historical record compatibility and packet checks for mandatory instructions.

The [temporary family preview receipt](verification/family-preview-2026-11-18.json) records a separate, static sample journey with Hurtle Turtle. The public family link depends on the workstation staying online. Ticket reading, payment, and mailing in that preview are simulated.

The [execution roadmap](ROADMAP.md) owns remaining tasks and acceptance criteria. This page owns the evidence summary and release decision. The [controlled-beta operating packet](operations/controlled-beta.md) owns the activation/recovery procedure. No source export, test run, GitHub publication or roadmap is itself deployment authority.

## Current source status

**November 25 paid capture.** The local candidate adds the hold-expiry disclosure
before authorization, decision-time Lob evidence, the original charge basis after
settlement, automatic full refunds for verified original non-mailing, and an
on-demand first-20 review. The customer and provider claims were checked against
Lob and FTC sources. Human counsel review, intended-target qualification and
production measurements remain open. [Paid timing and follow-up](operations/controlled-beta.md#paid-charge-timing-and-exact-letter-follow-up)

**November 22 code review.** The customer checkout can recover an uncertain card hold after reload. The mail deadline calculation now skips printer closures throughout production, and the private backup monitor measures the age of the last replicated source snapshot. The [repository review](verification/code-review-2026-11-22.json) records fixes, regression checks and outstanding evidence. Paid release still requires deployed qualification.

**November 22 handoff.** The launch-fixes checkout's application and deployment files matched `main@e27e151` before this increment. A corrected Gemini image-token estimate let Flash-Lite read all 11 printed fields on the original synthetic ticket through the customer API in a scratch process on the P06 VM. The experiment used an isolated database and an in-process test mailbox. Automated review failed. On a separate San Francisco fixture, the customer-supplied review path confirmed the supplied facts and intake stopped before payment with no verified official page. The full CISA registry reproduced the San Francisco redirect refusal. A San Diego registry-only probe reached the model and stopped on a provider error. The joined sandbox hold, letter and recovery drill remain open. [Rehearsal and local verification](verification/p06-r05-2026-11-22.json)

R05 inventory now covers 98 explicitly classified application tables across service and monitor snapshots. All 82 application tables present in the sandbox service database had a classification. In an isolated access test on the VM, prefetch left a fresh link usable, expired and consumed links returned 401, and logout revoked the session. The test used a local mailbox. Retention periods, provider and backup copies, actual email delivery and revocation on the standing target still require evidence. [R05 verification](verification/p06-r05-2026-11-22.json)

The full backend suite passed 13,934 tests with one dependency deprecation warning. The affected suites also passed after the final edits. The three fake-provider browser campaigns passed 155 paid-flow checks, 14 paid OCR checks and 55 mobile OCR checks, with zero skips, retries or failures reported. All three ran on application fingerprint `6f73f029…`. Lint, type checks, offline qualification and the production web build passed. [November 22 test evidence](verification/p06-r05-2026-11-22.json)

**November 21 candidate (`main@d139c39`):** the provisional guarded-paid line is merged, and the
decline-window rule is review-corrected — every in-scope decline timestamp must parse as
timezone-aware, malformed values raise `StorageCorruption`, and a valid recent decline cannot mask a
corrupt row (regression test included). Local candidate evidence: ruff clean, format clean (499
files), mypy clean (499 files), **13,898 backend tests passed with 0 skipped and 0 failures**, and
**three browser campaigns passed (paid 155/155, paid-OCR 14/14, mobile-OCR 55/55, 224 checks)** on a
shared application fingerprint `2871be0a…`. The 2026-11-21 [increment receipt](verification/increment-2026-11-21a.json)
records the Cloud Build image-reference fix (candidate-scoped tags, atomic `:staging` tag retired),
the Secret Manager migration of the deployed environment, and 308 passing R04/P03/R05/R08-readiness
tests. R01's remaining acceptance item is CI/test/promotion configuration, not the candidate itself.

The November 17 publication candidate extends commit a717d5ab08dbed349406e0171bc29eddec3419c8 with journey-ledger, OCR/manual-review observability, T01 reconstruction, mandatory OCR and documentation work. Its exact source manifest and checks are in [the publication receipt](verification/publication-2026-11-17.json). The ledger, OCR/manual-review slice, T01 read-only reconstruction and required OCR-attempt gate are implemented and locally tested; see [Telemetry Foundation](TELEMETRY-FOUNDATION.md) for scope, atomicity, replay guarantees and limitations. No persistent analyzer, exporter or autonomous repair is live. That candidate used fake providers for the paid beta, with both BETA and PAID admission gates. The November 23 ordinary-mail pilot has separate qualification and enrollment.

The product remains clerical. Essential unknown facts block charge/dispatch; the customer supplies and confirms their own facts and reasoning. Do not infer legal conclusions, silently substitute providers, reopen retired unverified-address overrides or create a city-by-city launch allowlist.

OCR selection is configurable. [OCR routing](OCR-ROUTING.md) documents the primary and optional secondary routes; a selectable model does not establish that it is configured or qualified in production. [Customer Choice Rules](CUSTOMER-CHOICE-RULES.md) owns guarantee/causation semantics.

## Evidence and its limits

On November 20, Stripe's ParkingBreaker sandbox returned three active one-time USD
prices on one certified-mail product. Three synthetic appeals used the local checkout
and Stripe's test card. Each assigned offer matched its manual hold ($19.99, $24.99
or $29.99). The PaymentIntent and Charge carried the matching experiment and arm.
Stripe search found all three originals. All three holds were released with zero
captured. The ParkingBreaker live account also returned all three active one-time
USD prices with matching experiment and arm labels. Stripe reported charges and
payouts enabled on that account. The live catalog setup made no payment requests.
Deployment of the payment labels, deployed provider qualification and paid
admission remain open. [Three-price reporting procedure](operations/controlled-beta.md#three-price-checkout-report)

The November 20 integrated worktree passed 13,885 backend tests and 169 paid
browser checks with synthetic providers. It limits new card attempts after repeated
declines, preserves original payment recovery, and reports recorded money and
unfinished fulfillment work. Support intake uses a separate credential. Customer
support codes stay hidden until the channel is qualified, and the retention planner
accounts for the support tables. A signed candidate, deployed provider checks and
staffed support coverage remain open. [Launch safeguards receipt](verification/launch-safeguards-2026-11-20.json)

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

On November 18, the physical A56 opened the native camera, restored a synthetic panorama alongside both ticket photos, and saved a ticket correction with the Android keyboard open. The family sample also completed the scene-photo path in portrait and supported scene capture in landscape. [Curbside device receipt](verification/android-curbside-2026-11-18.json)

Historical A56/Android fake-provider tests exercised front/back file selection, cancel, missing-fact blocking, confirmation/reload, edit invalidation, bad-file refusal, injected connection failure, background/return, actual camera and native gallery. The camera image was fictional; stored pixels and dimensions survived the metadata-stripping filter. Raw-byte identity was not claimed.

Those results used older recorded fingerprints and a local transport bridge. The recent A56 family sample also exercised landscape. Physical iPhone/Safari and rotation on the deployment candidate remain open. Reinspect devices, processes and ports; obsolete handoff PIDs are not current runtime facts.

## Release gates

Task IDs resolve to the authoritative roadmap.

| Gate | Current state | Next evidence |
| --- | --- | --- |
| Current candidate / CI | November 20 local R01 pass on `f0a0bf8` plus the uncommitted three-price checkout slice (`97fffcc53501`): `13,853/13,853` backend tests, `ruff`/`mypy` across `492` files, both qualification self-tests, `make web-check`, `cloudrun-private-vm` deploy dry-runs, and `224/224` browser checks (`14` paid-OCR, `55` mobile-OCR, `155` paid, zero skips/flakes). [R01 local candidate receipt](verification/r01-candidate-2026-11-20.json). Earlier Nov 18 WebKit and paid-cap evidence in [WebKit evidence](verification/expanded-launch-checks-2026-11-18.json) and [Build and checkout evidence](verification/paid-cohort-2026-11-18.json). | R01 still requires a reviewed candidate freeze, a scoped build window and the immutable image digests. Read-only Cloud Build and registry inventory access succeeded on November 20. |
| Hurtle customer journey | Ticket photos and OCR come first. The family sample shows five curbside illustrations under the shared photo prompt. The A56 traversed all five and returned to the sample scan. [Family preview evidence](verification/family-preview-2026-11-18.json) | Local review only. The signed cloud rehearsal predates this frontend. |
| OCR / research | Four real DeepSeek Flash reads through local intake matched all expected field states and values across four synthetic tickets. Each result was still unconfirmed on readback. [Clear-ticket receipt](verification/p04-deepseek-single-2026-11-18.json) and [expanded checks](verification/expanded-launch-checks-2026-11-18.json) | P01/P04 still require deployed configuration, customer correction and confirmation, and source-backed filing or refusal in the same case. |
| Official evidence | 23 qualification rows retain uncertainties; 109 saved quotes historically checked | P04: resolve relevant procedure/evidence gaps; no fabricated source facts |
| Physical devices / access | WebKit 26.0 saved and restored all eight repository fixture scenarios in mobile emulation. Portrait and landscape layout checks passed. Android hardware evidence is source-specific. [Browser evidence](verification/expanded-launch-checks-2026-11-18.json) | P02/P05 require current physical iPhone and Android camera, rotation, network recovery and delivered access-email checks. |
| Target / security / recovery | The corrected signed gateway served the original pinned fake build over trusted private TLS and authenticated public ingress. It passed lifecycle readiness, three intake-pause refusals, and a full VM stop/start with one disk attachment and the synthetic workspace intact. The enabled backup service copied exact live test assets off host when manually started. The independent restore matched SQLite and artifact hashes and kept quarantine. An external workstation probe recorded failure and recovery. Gmail received the matching Cloud Monitoring outage alert about 4 minutes 19 seconds after the VM stopped and also received the recovery notification. [Target receipt](verification/r02-target-preflight-2026-11-18.json) and [local cutover checks](verification/r02-cutover-2026-11-18.json) | Responder acknowledgment and support coverage remain unverified. Original Stripe/Lob reconciliation is open. The $25 rehearsal covered synthetic data only. |
| Printing / webhooks / money | Stripe replay returned the same sandbox hold. Two Stripe CLI–forwarded events passed signature and duplicate checks after case binding. All test holds were released with zero captured. One Lob test letter rendered a six-page certified proof. The four approved content pages and their order were verified. [Provider evidence](verification/expanded-launch-checks-2026-11-18.json) | P06 still requires a Lob-signed callback, deployed callback delivery, hold expiry and payment/mail reconciliation on a joined case. Physical print and delivery remain unobserved. |
| Outcomes / support | Local mechanisms exist; coverage and real operation unqualified | J05/P07: all-appeal follow-up, primary/backup support, real alert receipt and acknowledgment |
| Privacy / telemetry | Journey ledger is restricted; full coverage/retention/export policies open | J01/J06/R05: coverage reconciliation and governed data lifecycle |
| Activation | NO-GO. The local checkout enforces a cumulative paid limit before a provider hold and preserves admitted cases across restart. [Capacity and regression checks](verification/paid-cohort-2026-11-18.json) | R06/R07 require the exact deployable artifact, target rehearsal, funded cohort size, dates, reserves and stop rules. |
| Paid beta | Required launch offering; not activated | R07: paid gates, configured price/disclosures/reserves and bounded paid cohort; R08: reconcile actual costs/remedies |
| Unattended improvement | Ledger, OCR/manual-review observations and T01 reconstruction tested; queue not implemented | T01–T07 local proof, then T08/R09 for qualified production release |

Historical cloud inventories do not establish current service health. No new deployment, charge, mail, outreach, ad spend or provider trial is authorized by this page. Consolidation and recovery provenance remain in [CONSOLIDATION.md](CONSOLIDATION.md).

The R03 restore tool now exports a restricted inventory of original provider lookup anchors and
approval fingerprints from the quarantined SQLite snapshot. A local mixed paid and no-charge
recovery fixture verified that the export preserves every restored file, omits beta consent from
Stripe lookups, and retains the Lob submission token when the local letter ID is missing.
The command returns `NEEDS_EXTERNAL` until the original provider records are checked during the
joined P06/R03 rehearsal. [Recovery procedure](operations/controlled-beta.md#recovery-and-rollback)

The mobile ticket and packet-review changes at `01a944e` passed `make web-check`
and all 14 paid OCR checks in `synthetic_rehearsal.py --service-phase paid
--ticket-reading-preview`. The browser inspected two pages of the exact PDF and
checked the approval and dispatch fingerprints. A fresh lockfile install also built the
frontend in test mode with a synthetic publishable key and verified the HTTPS API
rewrites and embedded key. The Cloud Build recipe now passes those settings through.
Registry images, provider accounts and the physical phone journey remain to be qualified.

## First three paid beta increments

[Local verification](verification/launch-increments-2026-11-17.json) records 13,756
passing backend tests for the isolated statement increment and 13,797 passing on the
integrated snapshot. New approval manifests hash the submitted intake statement. A
corrected remail needs explicit approval of its PDF and envelope before a new provider
create. The full refund for a proven system address error is independent of remail.

The joined paid OCR browser run passed 13 checks, including one simulated capture and
an exact mailing record, and one unverified office case that released its hold without
preparing mail. The legacy paid and free OCR runs passed 152 and 53 checks respectively.
These results support local P01/P03/P06 behavior. Provider, device, production target,
operational coverage and activation gates remain open.


## November 17 capture-start milestone

[T02 capture-start provenance](verification/journey-capture-start-2026-11-17.json) is
implemented and locally tested: identity commits atomically with new workspaces or the
first newly recorded pending OCR/review attempt, legacy migration preserves unknowns,
and scoped/immutable/retention/recovery checks pass. The recorded candidate passed
13,768 backend tests, frontend lint/types/build, both offline qualification self-tests,
152 paid browser checks and 53 required-OCR mobile-emulation checks with zero browser
skips/flakes. The receipt preserves failed test-environment attempts and their passing
retries. This does not complete T02, prove complete attempt history, configure runtime
identity, change the deployed preview, or close a live-provider/device BETA/PAID gate.
Live paid beta remains **NO-GO**.
