# ParkingBreaker execution roadmap

Updated November 18, 2026 (Pacific). This is the authoritative remaining-work plan for the current checkout. It replaces the raw external review dump and the stopped Figma handoff. Product rules remain in [Customer Choice Rules](CUSTOMER-CHOICE-RULES.md), [OCR routing](OCR-ROUTING.md), and the source; release evidence remains in [beta readiness](BETA-READINESS.md). Historical receipts qualify only their recorded inputs.

**Current release state: NO-GO for live paid beta. Preview is deployed at [parkingbreaker.com](https://parkingbreaker.com) with fake providers; see [deployment evidence](verification/deployment-preview-2026-11-17.json).** The durable journey ledger and T01 read-only reconstruction are implemented and locally tested. Full capture provenance, durable attempt history, persistent analysis, autonomous repair, marketing measurement, and production qualification remain open. Owner direction: beta is paid; new intake requires ticket images and OCR before customer confirmation. The intended correct-office/on-time commitment still requires live qualification. A roadmap is neither a deployment approval nor proof that a business will be profitable.

## Decisions already made

1. Extend the existing authority. Transaction-store writes, append-only case history, pure appeal-event derivation, and the evidence-backed money ledger remain authoritative. No parallel transaction system or estimated financial ledger.
2. Retain the existing single-writer SQLite deployment design and fencing. Qualify the private-VM target before considering a storage rewrite.
3. First-party durable events are the operational evidence; case/payment records remain authoritative. Slack is the business observatory. GA4 is an optional, restricted downstream projection. Neither can authorize payments, mailing, refunds, or destination changes.
4. Start analysis from durable hooks and a persistent queue, with periodic reconciliation for missed triggers. Amir never needs to relay an incident between AI chats to initiate diagnosis.
5. Calculate known quantities in deterministic code. Use qualified AI for diagnosis, prioritization, experiments, and decisions with uncertainty. Record its evidence, assumptions, model version, cost, authority and measured outcome.
6. Reuse existing automatic remedies when their evidence requirements are satisfied. Missing causation never becomes customer fault. Do not add a Slack approval delay to a proven system-address-error refund.
7. Keep unsupported essential facts and unverified destinations stopped. Do not reopen the retired customer-address override, create a blanket city allowlist project, or invent legal arguments.
8. Follow the [paid beta execution brief](#paid-beta-execution-brief) for the next qualification task, acceptance criteria and external authority.
9. Default new paid-provider trials, ad spend and production activation to unconfigured until a concrete standing budget and action scope exist. Old one-off approvals and historical budget suggestions are not ongoing authority.
10. Make no new product rewrite or Figma redesign a launch dependency. Keep the working app; use design work to resolve measured usability defects.
11. Beta is a paid maturity phase, not a free offer. Use the existing paid flow and meet BETA plus PAID gates before admitting customers. Preserve historical no-charge commitments; do not select a free entitlement batch for the current launch. Require the exact ticket images and a completed OCR attempt before new confirmation/submission. Correction may follow failed OCR; it cannot turn failure into verified facts. Accept only when official office, filing rule/deadline and supported timely fulfillment are established; qualify the promise and remedy before publication.

## How a coding model executes this plan

Read AGENTS.md, the current worktree diff, this document's next ready task, and the named source/tests. Preserve unrelated edits. Verify each named interface before coding; a path below identifies a surface, not permission to rewrite the entire module.

For each task, state a short brief in the working response: observed behavior, intended behavior, affected files, invariants and acceptance checks. The selected worker implements and tests that task directly in its assigned checkout. Keep one writer on this checkout; another model reviews after implementation stops. Preserve unrelated edits. Use a separate checkout only when concurrent work is explicitly assigned. Do not spend a second implementation pass on a handoff document.

Keep five separate evidence states: **implemented → tested → configured → live → measured**. A task may be locally tested while configuration and live evidence are absent. Record source digest, commands and results, configuration identity without secrets, evidence location, unresolved limitations and the next task. Do not call a mocked provider test a live qualification. Do not weaken tests or thresholds to get a green result.

Use focused tests for isolated changes. Storage, authorization, concurrency, money, mail and retention changes also need the relevant existing recovery/atomicity/integrity suites. Run the full release gates on the final candidate, once it is stable; do not repeat them after every documentation edit. The repository commands are in README and Makefile.

A task is complete only when its evidence is inspectable and its limitations are explicit. Failed or unsupported diagnosis remains unresolved. No commit, publication, deployment, customer contact or spending authorization is supplied by this document itself.

## Paid beta execution brief

**Next action:** freeze one release candidate with the current customer flow and corrected gateway, then run R01 on that source. Use the resulting artifact for the deployed P04 and P06 checks. P04 needs qualified provider accounts and official-source evidence. P06 needs a joined sandbox case whose original provider records can also support R03 reconciliation. T02/T03, Slack, GA4 and marketing stay queued unless a specific missing operational record prevents safe recovery or verification.

Statement fidelity, fresh approval for a changed remail address and the joined paid OCR journey passed [local verification](verification/launch-increments-2026-11-17.json). Preserve the driver's submitted wording, selected evidence and exact PDF/envelope approval. Keep historical no-charge obligations, independent wrong-address refunds and the stop before fulfillment for unsupported cases. Paid admission still requires R06/R07 and their existing dependencies.

The November 18 isolated cloud rehearsal qualified the corrected gateway on the original
signed fake-provider build. The app passed private TLS and authenticated public health
checks. It recovered from a full VM stop and restart with the same disk and synthetic
workspace. The off-host backup restored with matching records and files and remained in
quarantine. An independent probe recorded outage and recovery, and Cloud Monitoring
opened and closed the incident. Alert delivery and responder acknowledgment are open.
Original Stripe and Lob reconciliation returned `NEEDS_EXTERNAL`. Both authoritative
nameservers cleared the rehearsal DNS record and the public test IP was released. The
approved $25 scope applied to this drill and its scheduled cleanup.
[Target receipt](verification/r02-target-preflight-2026-11-18.json) and
[local cutover evidence](verification/r02-cutover-2026-11-18.json).

The [clear-ticket OCR receipt](verification/p04-deepseek-single-2026-11-18.json) and
[expanded launch checks](verification/expanded-launch-checks-2026-11-18.json) cover four real
DeepSeek readings through local intake. A missing plate stayed absent and a masked citation
number stayed unreadable. The same checks cover mobile WebKit, a restored upload, a released
Stripe sandbox hold and a rendered Lob certified-mail proof. P04 still needs the deployed
reading, driver correction and official-source decision in one journey. For P06, exercise
Lob-signed callbacks, deployed callback delivery and hold expiry on a single joined case.
The local callback route already accepted and deduplicated two CLI-forwarded Stripe sandbox events.

### First deliverable — R02 target and local preflight

Use `PB_DEPLOY_TOPOLOGY=cloudrun-private-vm`. The **design target** is project `neuraldraft-parkingbreaker`, region `us-west1`, private VM `pb-qual-backend-01` in `us-west1-a`, and regional disk `pb-qual-data-01` replicated in `us-west1-a` and `us-west1-b`. These are qualification names to verify against current inventory. Use an isolated frontend revision and synthetic data. Keep customer traffic on the existing preview. Give only the VM write ownership of the ext4 volume. Build the frontend with the private gateway's trusted HTTPS origin on port 8443. Use the [target runbook](operations/controlled-beta.md#target-security-acceptance), [frontend template](../deploy/cloudrun-frontend.service.yaml) and [VM overlay](../deploy/compose.private-vm.yaml).

1. From repo root run the commands below on one working-tree fingerprint. Save exit status, counts and the deploy/rollback plans. The checks exercise the disk policy, overlay, old-owner fencing, lifecycle and readiness locally.

   ```sh
   bash deploy/validate.sh --dry-run
   PB_DEPLOY_TOPOLOGY=cloudrun-private-vm bash deploy/lifecycle.sh deploy --dry-run
   PB_DEPLOY_TOPOLOGY=cloudrun-private-vm bash deploy/lifecycle.sh rollback --dry-run
   (cd backend && PB_PROVIDER_MODE=preview .venv/bin/python -m pytest tests/test_t22_disk_policy.py tests/test_t22_cloudrun_overlay.py tests/test_t22_lifecycle.py tests/test_release_readiness.py)
   ```

   [Host provisioning](../deploy/provision-host.sh) now prepares the private gateway rule for the selected frontend subnet and VM address on TCP 8443. Verify the effective path from the deployed frontend and refusal from outside it. [Scheduled backup](../deploy/backup.sh) now selects the private overlay and checks the network replica mount on the host and inside the copy container. Prove the actual timer transfers the bundle to its intended independent receiver. Use the provisioner's mount `/srv/parkingbreaker`, its `data` subdirectory and its root-owned 0600 environment file outside Git.

2. Write `docs/verification/r02-target-preflight-YYYY-MM-DD.json` using the drill date. Link it from R02 and [beta readiness](BETA-READINESS.md). Record the exact candidate source/immutable image digests or their build blocker, VM size, disk type/size/mount, service accounts and grants, frontend revision/origin, private DNS/TLS, firewall source, off-host backup destination, previous owner/rollback image and operator. Cite a dated observation for each existing resource. Mark proposed names and missing evidence explicitly.
3. Price **one 24-hour synthetic drill** including both frontend containers, VM, regional disk, backups, network and any resources retained afterward. Give Amir the exact actions, project, UTC window, upper dollar limit, shutdown steps, residual costs and resource disposition in one approval request. The owner approved **$25 total** for the November 18–19 synthetic rehearsal. The seven-day backup expires November 26 at 4 p.m. PST. On the approved target, R02 passes when the disk reports exactly one attached writer, the old owner is fenced, data survives restart, the reviewed image serves over trusted private TLS, public internal/worker paths are refused and lifecycle readiness observes a fresh monitor poll and successful sweep.

### What each remaining drill must prove

| Order / task | Procedure | Pass condition and evidence |
| --- | --- | --- |
| 2 · R03 recovery | Run `bash deploy/backup.sh --dry-run` first. On the approved isolated target, seed an approved synthetic packet and pending provider effects. Take the scheduled backup, copy it encrypted off host, fence the old owner and restore from that copy into a fresh empty destination. Keep `RESTORE_QUARANTINE.json` in place. | Verify the database and artifact manifest, selected evidence, PDF/envelope hashes, approvals, history and pending effects. Measure recovery point from simulated failure to the newest recovered committed data and recovery time from failure until safe resumption. Drill limits are 60 minutes of recovery-point lag and 120 minutes to resume, with every accepted filing deadline protected. Resolve uncertain mail/payment against original Stripe/Lob test objects from P06. Require zero duplicate sends/captures. An unresolved effect or the CLI's `NEEDS_EXTERNAL` leaves R03 open and the restore quarantined. [Recovery procedure](operations/controlled-beta.md#recovery-and-rollback) |
| 3 · R04 incident controls | Use a probe hosted outside the qualification VM. Exercise full host loss, stalled scheduler/sweep, stale ownership, queue backlog and disk pressure. Separately stop new paid authorizations, new free admissions and all new intake. Include an existing case, a signed callback and a recovery operation as controls. | Observe host-loss alert delivery within five minutes and record the named responder's acknowledgment and timestamp. Each stop refuses its intended new operation with zero provider side effects. Once the host is available, existing status, callbacks and reconciliation still work under the intake stops. Record before/after control revisions, recovery and alert clearance. Use the existing health thresholds for scheduler and queue signals. [Stop procedures](operations/controlled-beta.md#intake-stop-and-incident-handling) |
| 4 · P04 OCR and official procedure | Freeze synthetic fixtures and expected outcomes before calls. Run one clear ticket, one ambiguous identifier, one safe refusal for an unsupported or unverified filing, and one customer correction after completed OCR through the intake/research coordinators. Freeze the [configured OCR routing](OCR-ROUTING.md), research model, search account, official-source dates and per-role call/cost caps. | The clear case reaches review with the correct office, purpose and deadline evidence. Ambiguity stays explicit. Refused cases produce zero capture or mailing. Corrections retain literal/adopted provenance and require fresh confirmation. Retain fixture hashes, expected/actual dispositions, quoted official evidence, call counts and reserved/actual or unknown cost. Stop on a wrong essential fact accepted as verified, an unverified destination allowed through, unknown spend or an exhausted cap. |
| 5 · P06 payment and certified print | Use owner-approved Stripe test and Lob test accounts. If R03 is waiting on provider records, run the minimum P06 setup first and reuse those same objects. Exercise one manual authorization, one eligible certified-mail flow, expiry/release and duplicate/out-of-order signed callbacks. Interrupt once at the provider-response/local-commit boundary and reconcile using the original IDs. | Verify account/mode, authorized amount, provider hold expiry and separate customer review deadline. Match the exact approved PDF and envelope hashes to the submitted request. Inspect every provider proof page for US Letter dimensions, legibility, evidence order, destination, return address and certified option. Require zero premature or duplicate captures or mail creates. Save restricted receipt references and show actual print/delivery as open wherever the sandbox supplies only a digital proof. [Print procedure](operations/controlled-beta.md#printing-and-notifications) |

A Lob test object or digital proof with only creation evidence must leave the hold uncaptured. Accept a capture only when the [submission evidence rule](../backend/src/parkingbreaker/lob_submission.py) recognizes the qualifying event. If the sandbox cannot supply it, record the passing refusal and leave the positive provider path open.

For each drill, retain the source/config fingerprint, fixture, expected and observed result, evidence mode (local fake, provider test, or deployed target), timestamps, failure/retry history and the exact missing receipt. Keep account identifiers and provider object IDs in restricted operational records and use references in Git. External provider trials require their own named accounts, fixtures, ceilings and approval. Physical mailing, live charges and admitting customers each require explicit scope. Preserve append-only history and version any necessary store migration. If a local check exposes a defect, fix and verify that increment before starting another. Keep paid activation **NO-GO** through qualification.

### Joined-journey execution detail (added November 19)

Use these steps to close the open customer-journey, provider, recovery, and activation gates. Obtain action-specific approval for spend, live charges, physical mail, and admission.

**A. Freeze the candidate (R01).** Record the exact candidate commit from `main` after integrating the unfamiliar-issuer checks. The signed rehearsal images (`50526bf`) predate the current frontend and paid-cap checkout. Use the existing Cloud Build recipes and record the image digests. Run `make check`, `make web-check`, `make qualification-check`, and the paid, paid-OCR and mobile-OCR campaigns against the recorded source and configuration.

**B. Unfamiliar-issuer API tests (P04, local).** `backend/tests/test_unfamiliar_issuers_api.py` exercises the customer API with synthetic tickets and hash-matched official snapshots. Cambridge preserves the originally blank plate and the driver correction, confirms the cited mail-in evidence under the scripted extraction, and releases the synthetic hold when an essential deadline fact stays unresolved. Keep the stage and scope guards for live provider output. Georgia Tech stops before a hold when the issuer cannot be verified through DotGov. A synthetic delegation in a separate test path reaches the frozen official online-only instructions and proves the payment wording cannot become a mailing destination. Both Georgia Tech paths assert zero authorizations and zero letters. Deployed provider qualification remains open.

**C. Deployed journey on the rehearsal-class target (P01/P02/P04/P05).** One owner-scoped case per run, evidence kept with the case id: camera intake on the physical A56 and an iPhone, real OCR, original reading plus driver corrections preserved, fresh confirmation, official-source decision, delivered access email. Run one fulfillable case and one that must stop; the stop must show the official procedure and produce zero hold and zero mail. Record device/OS/browser, build fingerprint, fixture hash, timestamps and observer. A fake or local result is labeled as such.

**D. Joined payment/mail/recovery case (P06/R03).** Use one Stripe test hold and one Lob test letter on the same case: hold amount and expiry, exact approved PDF and envelope hashes against the provider request, signed Stripe and Lob callbacks delivered to the deployed endpoint, interruption at the provider-response/local-commit boundary, then recovery by the original provider ids, then replay proving no second letter or capture. A digital proof with only creation evidence must leave the hold uncaptured. Close R03 only after the original-object reconciliation, not the local `READY_FAKE` result.

**E. Alert and support (R04/P07).** Deliver one test alert to a named human responder and record the acknowledgment time. Ask Amir to select primary and backup responders, coverage hours, and an escalation channel.

**F. R07 decision packet.** One page, from evidence only: exact release (commit, image digests), price and customer disclosures, the correct-office/on-time promise and remedy as qualified, support owner and backup, funded reserve, `PB_PAID_COHORT_CAP` value and the dates it applies, stop conditions (paid-authorization stop, intake pause, incident hold) with who may pull them, and the first-cohort reconciliation plan (captures, refunds, remails, postage, AI cost, outcomes) that gates any expansion. State recommendation and the evidence gaps that remain; list Amir's required decisions separately from engineering results.

### Repository verification conventions

1. Inspect `git status --short`, the relevant diff and the next task. Read only its named modules and tests. Do not reset the checkout, rebuild completed work, alter historical receipts or chase every old suggestion.
2. For the remaining T02 automation work, inspect `journey_events.py`, `journey_timeline.py`, workspace/intake creation in `transaction_store.py` and `ticket_intake_store.py`, and the existing journey tests. Record build/config identity and capture-start provenance without inventing values for old rows. Extend migration and retention coverage for any new stored fields. Prove migration/restart, duplicate creation, rollback, owner isolation and legacy LIMITED coverage before advancing.
3. When automation resumes, complete one coherent change at a time in T02 → T03 → T04 → T05 → T06 → T07 order. T03 and T04 share a prerequisite. T07 is the first autonomous-repair proof. Use existing protocols/stores before adding an interface. Keep AI diagnosis downstream of recorded facts.
4. Prioritize P01–P08 and R01–R06, with B01, actual obligations from M02, and the reserve and cap calculation needed by R07. The full acquisition metrics under M03, observatory and repair automation can proceed separately. External credentials, devices, provider receipts and staffing are real dependencies: record the exact missing evidence and continue independent authorized code work. Never mark an external gate complete with a mock or a proposal.
5. For each change, run its focused tests first. Storage/money/mail changes also require the named recovery, atomicity and integrity suites. Backend commands are `cd backend && uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run mypy`, and `PB_PROVIDER_MODE=preview uv run pytest <focused tests>`. Keep commands in the correct working directory; use the installed Python 3.12 environment when dependency sync is unnecessary.
6. Run frontend lint/types before a build or after it finishes. Next.js builds rewrite generated types; do not run `tsc` against that same output concurrently. Run the supported paid browser campaign after customer-flow changes: `backend/.venv/bin/python build-kit/scripts/synthetic_rehearsal.py --service-phase paid --out <new ignored output directory>`. Also run the required-OCR mobile campaign with `--service-phase controlled_free_beta --browser-project chromium-mobile375x812 --ticket-reading-preview` and a separate output directory. The latter exercises the historical no-charge route as a regression fixture, not the paid launch offering. For the joined paid OCR check, add `--ticket-reading-preview` to the paid command and use a separate output directory. It selects the required-reading suite. The broad paid legacy campaign remains a separate regression check. Use synthetic providers and inspect both receipts for failures, skips and missing results.
7. Keep test databases and recordings out of Git and build uploads. Use disk-backed test storage for the full backend suite; run the Unix-socket fixture separately with a short temporary path if needed. Preserve failed checks in the receipt, fix the cause and rerun the affected gate. A success envelope is insufficient: inspect exit status and test totals.
8. Update the existing task row and readiness evidence when acceptance passes. Record commands, source hashes, results and remaining limits in one receipt per accepted milestone. On the stable release candidate run the full backend suite, frontend lint/types/build, qualification self-tests and relevant browser checks once. Re-run only when a new edit or failure invalidates a result. Never weaken trusted tests, omit a failing test from the release total or claim deployment without observing the serving revision.

Finish with the actual diff, passed/failed checks, deployed state if any, unresolved evidence and next ready task. Local implementation and testing can proceed. Use the exact target, action, budget and time window authorized for each external drill.

## Starting inventory

| Area | Present in source / local evidence | Remaining truth |
| --- | --- | --- |
| Case and payment authority | Append-only pb_case_history, transactional writes, pure oplog/appeal_events.py and ledger.py | Preserve; qualify deployed behavior and reconcile external facts |
| Journey foundation | Seven event kinds, five success hooks, opaque identity, same-transaction append, replay conflict detection, bounded internal reads | T01 bounded, pure timeline implemented; identity now starts at new workspace creation or the first newly committed durable OCR/review attempt; capture remains limited. Attribution, durable attempt history, exporters and repair worker remain open |
| Intake and OCR | Literal/customer-supplied fact bindings, manual correction, bounded confidence routing, provider budgets | Latest configured provider combination and complete live customer path still need qualification |
| Exact packet and funding | Existing approval, hash, manual-capture, beta and fulfillment controls | Real provider printing, webhook and expiry evidence still open |
| Recovery and monitoring | Ownership locks, readiness, scheduler, recovery CLI, retention inventory | Target restore, external reconciliation, total-host-loss alert and staffing proof open |
| Local verification | [November 17 publication candidate](verification/publication-2026-11-17.json): 13,754 backend tests, lint/types/format, production frontend build, 152 paid browser checks, 53 OCR mobile checks and 3 telemetry tests | [Preview deployed](verification/deployment-preview-2026-11-17.json) at parkingbreaker.com on commit 0bf011b with fake providers and required synthetic OCR. Live provider/device, durable production target and paid-launch qualification remain open |
| Slack / analytics | Observatory intent and channel setup context | No current durable exporter or production analyzer proven |
| Product readiness | Historical Android and synthetic browser evidence | Current candidate, real iPhone/Safari, landscape, provider and deployment gates remain open |

See [Telemetry Foundation](TELEMETRY-FOUNDATION.md) for the implemented schema and exact hook semantics.

## Priority and release gates

Task state **TESTED** means the bounded local acceptance passed (not production qualification); **EXTEND** means a relevant implementation exists; **BUILD** means the capability is missing; **QUALIFY** means real evidence is required; **DECIDE** means an external constraint or standing authority must be made concrete.

Gates: **BETA** = before controlled customer beta; **PAID** = before paid customer activation; **AUTO** = before unattended repair/release or business actions; **SCALE** = before materially increasing acquisition; **OPTIONAL** = not required to operate.

T01 is locally tested: it combines committed milestones, OCR decisions and client observations without business writes, with explicit capture gaps and snapshot-bound pagination. The automation sequence remains T02 → T03/T04 → T05 → T06 → T07. Prove one bounded repair early. The mandatory OCR attempt gate and paid example configuration are also implemented. Advance BETA and PAID qualification for the paid beta; launch does not require every AUTO or SCALE feature. Expand the catalog and economics alongside launch qualification; do not delay the proof until every dashboard exists.

## A. Durable truth and the first autonomous repair

| ID / state / gate | Scope and dependencies | Acceptance evidence |
| --- | --- | --- |
| T01 · TESTED · AUTO | `journey_timeline.py`, existing event/observation readers and `SqliteTransactionStore.journey_timeline`; no business writes. [Receipt](verification/journey-reconstruction-2026-11-17.json). | Deterministic source-order-preserving timeline, immutable snapshot pagination, replay/conflict checks, missing/late/limited/legacy/skip classifications. Existing no-charge journeys have no paid hold; corrected facts do not fabricate successful OCR. Version-one provenance remains LIMITED; legacy/complete classifications require evidence. Read-only/owner boundaries and frozen paid/beta records tested. |
| T02 · EXTEND · AUTO | Versioned event contract and identity at workspace creation/first durable attempt. Depend on T01. [Capture-start slice tested](verification/journey-capture-start-2026-11-17.json): immutable provenance and atomic identity at workspace creation/new OCR or review claim; historical rows remain unknown/LIMITED. Full event context and runtime build/config wiring remain open. | Build/config version, route/funding, closed status/reason, operation/attempt/causation, duration basis and capture coverage are recorded. Known/unknown/not-applicable remain distinct. Migration preserves old rows; old journeys report limited coverage. No arbitrary free text, credentials or customer content. |
| T03 · EXTEND · AUTO | Durable start/attempt/failure/retry/recovery accounting using existing intake authorization/operation records; T02. | A rolled-back business transaction leaves no fabricated success. Attempt-state changes and their immutable transition records commit together. Separately committed start/outcome evidence survives an unrelated business rollback; overwritten intermediate states cannot be recovered by polling. Crash gaps remain pending/unknown until reconciled. Recovery reconciles uncertain side effects rather than repeating them. Fault tests cover before-call, during-call, after-response and before-commit boundaries. |
| T04 · BUILD · AUTO | Transactional outbox and per-consumer checkpoint/lease; T02. Use the same authority transaction for committed event notification. | Crash/restart, lease expiry, slow consumer, duplicate delivery and backlog tests pass. Delivery attempts are separate from business facts. Local consumers are idempotent; external ambiguous delivery stays unknown/reconciled. No HTTP call inside a business transaction and no promise of exactly-once remote delivery. |
| T05 · BUILD · AUTO | Persistent analyzer and deduplicated repair queue; T01, T03, T04. Deterministic integrity rules run first; AI gets a restricted evidence bundle. | Event-triggered and catch-up scans create one actionable job per regression signature, with affected journeys/builds, severity, evidence, first/last occurrence and confidence. Leases, retry limit, cooldown, cost cap, dead-letter state and restart recovery work. Lack of evidence cannot produce an asserted diagnosis. |
| T06 · BUILD · AUTO | Engineering coordinator in isolated checkout; T05 and current AGENTS.md. | Job becomes a bounded synthetic reproduction and a patch with actual model/check receipts. Logs and customer text are data, never instructions. Worker has no production secrets or live mutation authority. Trusted integrity checks cannot be weakened by the proposed patch. A failed check leaves the job unresolved. |
| T07 · QUALIFY · AUTO | Controlled synthetic failure on one instrumented transition; T06. Use a throwaway fixture/checkout, not a customer failure. | Injection → durable evidence → analyzer job → failing regression → isolated patch → passing regression and relevant recovery/integrity suite → evidence linked in engineering journal. Restart/replay creates no duplicate job or business effect. No notification or second chat is needed from Amir. Record which step is actually AI-driven; a scripted repair is not proof of autonomous diagnosis. |
| T08 · BUILD · AUTO | Qualified release coordinator; T07 plus R01–R06 and approved release policy. | Immutable candidate, provenance, trusted gates, staged rollout, observation window and rollback work. Local verified repair and production-deployed repair have distinct statuses. No silent auto-merge, financial-policy edit or live deployment without applicable standing authority. Automatic containment may stop new activity while preserving existing obligations. |

T07 is the first end-to-end acceptance milestone. It proves a verified local repair, not an unattended production release. T08 adds production release only after its prerequisites.

### Attempt history and transaction boundaries

Clarification from the November 17 source review: these are complementary records and derived views, not two cloned requests or a new competing authority.

- A failed business operation can produce a successfully committed fact about that failure. The current journey ledger already includes ticket_read_refused and ticket_read_unknown. The rule is that a rolled-back mutation cannot leave a claimed success; it is not that the ledger may contain only happy-path events. Financial authority remains the existing recorded case/payment facts and ledger.py.
- The existing OCR claim is committed pending and later completed in place. Authorization rows also change state/revision in place. They are useful recovery records, but polling their latest values cannot reproduce every intermediate transition. Never describe a join of these tables as a complete, unedited history.
- T03 must preserve allowed attempt transitions durably, atomically with each corresponding attempt-state update, reusing existing append-only mechanisms where suitable. A separately committed start remains visible if later work rolls back. A crash before an observed outcome is recorded leaves pending/unknown, not an inferred decline. Reconcile provider evidence before retrying a side effect.
- Use sequential short transactions through the existing writer. A second SQLite WAL connection is not an independent concurrent writer and must not be used to sneak a failure log write through a still-open write transaction. An outbox row inside a rolled-back transaction also rolls back; failure evidence must belong to its own successfully committed attempt/outcome transaction.
- Export only a closed allowlist of generated identifiers, event/reason codes, counts and measurement metadata. Do not first save raw invalid payloads or exception messages and hope a downstream scrubber removes all PII. Existing restricted result_json, request_json, owner and payment fields are not an external telemetry payload.
- The background projector joins durable facts by restricted internal linkage, preserves per-source sequence/revision and causation, and reports unknown coverage. Wall-clock sorting is a presentation choice, not proof of causality under skew. It does not invent early validation failures, browser actions, overwritten states or a journey identity that was never recorded.
- The timeline projection, Slack/analytics exporters and repair queue are separate consumers. All can run continuously with bounded lag; asynchronous does not mean a nightly offline batch. Repair hypotheses stay in their own downstream journal. One failed exporter must not stop analysis.

Acceptance additions for T01/T03/T04: exercise a transition completed between two poll cycles, crash after attempt-start but before outcome, rollback of outcome persistence, duplicate recovery, invalid input before claim creation, clock skew and legacy missing linkage. Each case must either reconstruct supported facts or expose the exact missing coverage. No zero-risk or complete-capture claim without that evidence.

The closest named patterns are a CQRS-style read projection and transactional outbox. [Lambda architecture](https://learn.microsoft.com/en-us/azure/architecture/databases/guide/big-data-architectures#lambda-architecture) means batch and speed processing paths; it is not a formal synonym for separating failures from committed milestones. [SQLite isolation](https://www.sqlite.org/isolation.html) retains a single writer even in WAL mode. See also [CQRS and outbox guidance](https://learn.microsoft.com/en-us/azure/architecture/patterns/cqrs).

### Measurement and safe-change contract

November 17 design clarification. Apply this contract within T02, J01–J03, M04, T06 and T08; it is not a second roadmap or a requirement to replace existing interfaces. The journey milestone schema still has one optional duration. The November 17 OCR/manual-review slice now adds closed OCR decision-reason counts, effective routing thresholds/version, separate monotonic extraction/escalation spans, bounded client review intervals, and pinned guidance assignments with actual exposure. Full build/prompt identity, application-wide decision capture, telemetry health, and autonomous repair/release remain planned. See [implementation and limits](TELEMETRY-FOUNDATION.md#ocr--manual-review-vertical-slice-2026-11-17).

**Capture these distinctions before broadening instrumentation.** Historical records cannot recover an unrecorded decision input or exposure later.

| Required evidence | Meaning / acceptance |
| --- | --- |
| Stable correlation | Opaque journey, operation, attempt, parent/causation and event identifiers; distinguish one operation replayed from a new attempt. Propagate across browser, API, job and provider boundaries without exposing bearer tokens. |
| Effective version and exposure | Record the actual component/config/prompt/schema/provider model and routing-policy versions used at the decision, including fallback/cache behavior. An experiment assignment is not proof its treatment was rendered or executed; record actual exposure separately. Missing provider-version detail stays unknown. |
| Decision facts | Closed decision name, eligible alternatives, selected route, reason/gate codes and bounded privacy-reviewed input features. Examples: field-confidence bucket, missing/conflicting-field count, image-size bucket, budget-remaining bucket, official-source freshness and supported route. Capture what was known then, not facts learned afterward. Never log raw prompts, ticket values or speculative model chain-of-thought. |
| Time intervals | Defined start/end and status for client upload, queue wait, provider processing, retry backoff, research pause, customer correction/review and fulfillment wait. Distinguish system work, external wait, customer-page activity and unobserved time. Open spans stay open/unknown; no close event means no invented finish or automatic abandonment. |
| Quality and economic outcome | Tie the actual exposure/decision to correction, completion, safe refusal, packet integrity, capture, invoiced costs, return/remedy and later agency/customer outcomes. Customer corrections are useful observations, not automatically verified ground truth. A shorter/fewer-click flow is not success if service errors or losses increase. |
| Reproduction and coverage | Record safe evidence references, schema/coverage version and missing/dropped/unsampled flags. Hashes identify bytes but cannot reconstruct them. Where permitted evidence is needed for diagnosis, use governed restricted artifacts and a reviewed synthetic reproducer; never mirror customer content into Slack or unrestricted engineering prompts. |

Timing rules: measure local elapsed time with a monotonic clock; retain wall-clock timestamps and uncertainty for cross-process/cross-device correlation. Parent/child spans can overlap: do not sum nested or parallel durations as if all were sequential. A visible/active page is only an activity proxy, not proof of attention, confusion or motive. Report unresolved journeys as incomplete observations rather than assigning a made-up completion time. Analyze distribution and cohort differences with sample counts, not only one average.

Keep the durable business/attempt history authoritative. Traces can add diagnostic detail using the [OpenTelemetry span and correlation concepts](https://opentelemetry.io/docs/concepts/signals/traces/); an SDK or sampled trace backend is not a replacement for the ledger. Required milestones, attempt-transition evidence and money facts must not be sampled away. Rate-limit optional observations and expose coverage loss. Explicitly measure telemetry write latency, storage growth, export lag and dropped/rejected observations so instrumentation itself cannot silently degrade checkout.

### Components that should be independently changeable

Use the existing interfaces, including llm.py protocols, ticket_ocr.py policy and config.py composition, before adding abstractions. A versioned strategy or ordinary function is enough where it gives a stable testable boundary; this does not require microservices, a new event bus, or arbitrary runtime code loading.

| Change surface | Evidence needed | Boundary |
| --- | --- | --- |
| OCR/provider/prompt strategy | Effective versions, route/gate reason, confidence/conflict counts, latency, usage basis and correction/completion outcome | Exact fact provenance and customer confirmation remain enforced; no guessed identifiers or silent provider substitution |
| Research/destination resolver | Official-source references/freshness, supported procedure, pause reason and time to resolution | An experiment cannot invent evidence or approve an unsupported destination |
| Customer copy, help and step presentation | Assigned variant plus actual rendering, action/validation counts, correction/review timing and downstream completion | Do not change the meaning of consent, guarantee or the exact approved packet |
| Review/reminder scheduling | Schedule/policy version, due/sent/received state, recovery and eventual outcome | Preserve consent, deduplication and real authorization deadlines; no unsupported urgency claims |
| Marketing creative and budget policy | Actual creative/offer exposure, attribution, mature captured contribution, reservations and liabilities | Existing funded caps, reserves and claim rules bind every variant |
| Analyzer prioritization and repair strategy | Evidence coverage, diagnosis confidence, model/version/cost and independently verified outcome | Its own explanation or self-scored confidence is not acceptance; trusted tests and financial/mail controls remain outside patch authority |

“Hot swap” here means select a qualified version at an explicit operation boundary, then stage and observe its use. Do not patch a running transaction or mutate an in-flight operation's effective configuration. Persist enough version identity to explain and safely resume work after a crash. Preserve a customer's recorded offer and packet adoption. A migration that changes a bound artifact must invalidate it and obtain the existing required reapproval; an emergency stop can still block future side effects regardless of pinned versions.

For every changeable surface, define typed inputs/outputs, invariants, version identity, synthetic contract fixtures, permitted replacement boundary, rollback conditions and observation metrics. Versioned settings also require validation and limits: changing a threshold is a behavior change, not a harmless configuration edit. [OpenFeature evaluation details](https://openfeature.dev/specification/sections/flag-evaluation/) provide useful vocabulary for variant/reason metadata without requiring adoption of a new service.

### Evidence that a change helped

T06/T08 and M04 must link original incident/exposure → proposed hypothesis → candidate version → offline replay/test → authorized limited rollout → outcome and retain/rollback decision. Replay uses synthetic or explicitly permitted fixtures, fixes time/config inputs where appropriate and disables payment, mail and other side effects. Model nondeterminism is recorded, not disguised as an exact reproducible result.

A shadow result can identify disagreements and estimate costs; it cannot show how a customer would have responded to unseen copy or prove a causal conversion improvement. Provider-backed shadow calls also incur cost and need applicable authority. Use randomized treatment/control where feasible, actual exposure records, sufficiently mature outcomes and preregistered guardrails. With little traffic, report insufficient evidence and use bounded learning budgets rather than declaring a winner. Multiple overlapping changes need separable assignment or a declared combined treatment.

Promotion must improve the declared goal while protecting integrity, failure/remedy rates, latency, total cost and customer completion. Rollback stops future exposure; it does not undo a mailed letter, settled charge or database migration. Use the existing recovery/compensation controls for those effects. [Canary release guidance](https://sre.google/workbook/canarying-releases/) informs staged evaluation; no specific platform integration is implied.

**First narrow proof:** instrument one OCR/manual-review path with effective policy/version, bounded decision facts and separated system/customer timing; reconstruct it including incomplete coverage; reproduce a synthetic defect; compare a candidate under the same fixtures; prove version selection and rollback do not alter existing approved work. This sharpens T07 without making every component pluggable before the first proof.


## B. Complete observable customer journeys

| ID / state / gate | Scope and dependencies | Acceptance evidence |
| --- | --- | --- |
| J01 · EXTEND · BETA | Event coverage manifest spanning web/app/page.tsx, TicketFactReview, LiveLetter, DestinationChoice, LiveFulfillment and backend services; T02/T03. | Every meaningful state change, attempt, refusal, pause, retry, recovery and terminal result has an owner, event definition, source authority and test. Browser observations are explicitly distinct from server-confirmed milestones. Synthetic free, paid, manual and failed journeys exercise the manifest. |
| J02 · BUILD · BETA | First-party browser observation ingestion; J01. | Upload selection/validation/cancel, step visibility, corrections, preview/signature, stop and timeout are captured with bounded acknowledgment/retry. Schema/rate/size limits, owner binding and deduplication prevent forged server facts. URLs, tokens, raw files, arguments and form contents never enter telemetry. CSP remains strict. Tab-close loss is reported as uncertain coverage, not invented activity. |
| J03 · EXTEND · BETA | OCR, research and destination watcher hooks in ticket_ocr.py, ticket_intake_service.py and case_service.py; J01. | Provider, confidence route, missing/conflicting field counts, budget reservation/usage basis, refusal and latency recorded without fact values. Destination pause carries controlled agency/category identifiers and evidence state. Official verification refresh resumes only through existing validated controls. Unsupported original-stub/wet-signature/notary requirements stop the sale. |
| J04 · EXTEND · PAID | Funding/review-window/fulfillment hooks; J01. | Separate offer, authorization attempt, hold confirmed, no-charge entitlement, review expiry/recovery, exact approval, provider submission, production, scan, capture, return, refund, dispute and remail. Record actual evidence time and idempotency identity internally. No hold counted as revenue and no beta journey mislabeled as failed payment. |
| J05 · EXTEND · BETA | Outcome follow-up for every eligible appeal, using existing scheduling, notices and notification stores; J01. | Durable cadence, retries, opt-out/consent rules and one follow-up per scheduled occurrence. Agency outcome, customer-reported outcome, delivery and clerical rejection remain distinct with evidence basis. Nonresponse is unknown, never dismissal or defeat. Fake-clock tests cover overdue, duplicate, bounce and recovery; configured/live receipts follow separately. |
| J06 · BUILD · BETA | Telemetry health and coverage reconciliation; T04/J01. | Compare authoritative state transitions with observed events and coverage version. Alert/queue on gaps, skew, schema rejection, consumer lag, dropped browser batches and missing scheduled work. Synthetic canaries and independent liveness catch a dead watcher. No self-reported “all healthy” without fresh evidence. |
| J07 · BUILD · AUTO | Restricted timeline API and inspectable operations view; T01/J01. | Owner/role checks enforced at HTTP boundary, bounded cursor reads, explicit gaps and delayed events, build/config context and links to restricted evidence. Authentication required; no workspace/case bearer token in Slack link. Access audit and cross-owner denial tests pass. |

“Every event” means every defined meaningful action and state, with a tested coverage contract. It does not mean recording every keystroke, personal field or arbitrary model explanation.

## C. Algorithmic business measurement and decisions

| ID / state / gate | Scope and dependencies | Acceptance evidence |
| --- | --- | --- |
| M01 · BUILD · SCALE | First-party attribution and immutable offer/price assignment; T02/J02. | Controlled campaign/source registry, first/last-touch definitions, unknown/direct distinction and offer version persist across the journey. Arbitrary UTM strings are normalized to registered values or unknown. Never change an authorized amount because a test arm changes later. Consent and identity policy apply before external export. |
| M02 · EXTEND · PAID | Evidence-backed cost/revenue ingestion around ledger.py and existing billing facts; J04. | Captures/refunds/disputes, processor fees, postage/remail, AI usage invoices, support cost and ad spend have unique source records, currency, period and reconciliation state. Estimated/reserved/billed are separate. Tiny AI costs retain sufficient precision; rounding used for budget safety is not billed cost. No duplicate refunds or ad allocations. |
| M03 · BUILD · SCALE | Pure metric engine over authoritative events/financial facts; M01/M02. Implement the definitions below. | Fixture totals match independent arithmetic; cents/decimal handling, currency separation, late invoices, zero denominators, missing cost and cohort maturity tests pass. Every number exposes numerator, denominator, time basis, cohort, version, completeness and source references. A partial subtotal is labeled incomplete. |
| M04 · BUILD · SCALE | Durable experiment registry and evaluator; M03. | Hypothesis, eligible population, allocation, primary metric, guardrails, decision method, minimum evidence and stop condition fixed before exposure. Persist assignment. Separate beta from paid populations. No winner on insufficient or selectively sampled data; attribution alone is not causal proof. |
| M05 · BUILD · AUTO | Business watcher → AI decision → policy action pipeline; M03/M04, T04/T05. | Hooks and periodic reconciler trigger analysis of conversion, pricing, CAC, contribution, cash obligations and failure costs. Structured decisions cite evidence and uncertainty. Idempotent action adapter executes only inside recorded scope/budget/expiry. Observation window measures outcome; stale, missing or contradictory evidence prevents expansion. |
| M06 · QUALIFY · AUTO | Quality/cost benchmark for decision models; M05. | Frozen synthetic/historical cases with independently checked arithmetic and correct actions. Compare factual errors, unsafe actions, unresolved rate, latency and billed cost. Use the highest qualified reasoning capability for consequential uncertainty, inexpensive deterministic routines for arithmetic/replay, and escalation when evidence warrants it. Record actual model; no unmeasured efficiency claims. |
| M07 · BUILD · SCALE | Business operating view and cash forecast; M03/M05. | Cohort conversion, settled contribution, incomplete costs, capacity, obligations, reserves and scenario runway are inspectable. Free beta displays subsidy/learning cost, not fictitious paid margin. Owner cash withdrawals and fixed overhead are separate from unit contribution. Unknown liabilities block an optimistic spend increase. |

### Metric contract

All monetary values need currency and basis. Do not mix currencies without a recorded exchange-rate source and timestamp. Ratios use eligible unique journeys/customers, not raw event counts; attempt reliability is a separate measure.

| Quantity | Definition and required caveat |
| --- | --- |
| Step conversion | Unique eligible journeys reaching next required milestone / unique eligible journeys reaching prior milestone, within the declared observation window. Branch rules determine eligibility. |
| Capture conversion | New captured customers / eligible acquired visits or journeys, with the denominator explicitly named. Holds are a separate leading indicator. |
| Review expiry | Journeys whose review expires / journeys entering review; also report eventual recovery and permanent noncompletion after a defined mature window. Do not multiply this loss twice into an already observed end-to-end rate. |
| CAC | Attributable acquisition spend / new captured customers. Zero customers means no finite observed CAC; report spend and zero conversions, not zero cost. Distinguish paid-channel, blended and allocation uncertainty. |
| Net revenue | Recorded gross captures minus recorded refunds and realized dispute losses, with overlapping refund/dispute reversals reconciled once. Payout settlement timing is a separate cash measure. |
| Pre-acquisition contribution | Net revenue minus postage, remails, processing, AI and other attributable variable service costs. Include costs of nonconverting journeys in cohort totals. |
| Contribution after acquisition | Pre-acquisition contribution minus acquisition spend. Do not subtract acquisition again if imported cost totals already include it. |
| Operating profit | Contribution after acquisition minus fixed operating overhead. Show cash owner labor and imputed owner labor separately, if both are useful. |
| Allowable CAC | Pre-acquisition contribution per captured customer minus target profit and declared overhead/reserve allocation per captured customer. Incomplete cost makes this provisional; negative allowance means acquisition cannot satisfy that target. |
| Allowable CPC | Click-to-capture probability × allowable CAC, when that CAC already allocates nonconverter costs. Otherwise subtract expected nonconverter cost per click exactly once. Use a conservative supported bound for spend decisions. |
| Cost of a defect | Recorded remedy/refund/remail/support cost plus separately labeled estimates of lost contribution. Do not book counterfactual loss as an actual invoice. |
| Cash runway | Available cash after committed obligations/reserves divided by supported forecast net burn. If burn is zero/negative or uncertain, report the condition and scenarios rather than a misleading finite or infinite number. |
| Expected repair value | Estimated avoidable loss × supported chance of successful correction minus implementation, inference, review and rollout cost; identify uncertainty and actual post-release outcome. |

The business watcher must be able to answer: what changed, how much evidence supports it, which customers/campaigns/builds are affected, what action has the best supported expected result, what it may cost, what authority permits it, and how success or reversal will be measured.

### Watcher and action map

| Hook or timer | Analysis | Bounded automatic response |
| --- | --- | --- |
| OCR refusal/conflict/budget threshold | Failure mix, correction completion, provider latency/cost by build and route | Queue reproducible regression; preserve manual path; no silent provider substitution |
| Destination pause or stale official evidence | Age, source sufficiency, eligible route and abandonment risk | Collect/cite official evidence and propose validated directory update; no invented address or bypass |
| Intake/hold/review/approval | Branch conversion, expiry recovery, cost per completion, integrity violations | Diagnose reproducible friction; queue patch or authorized experiment; no automatic change to accepted packet/amount |
| Mail/capture/return/refund/dispute | Fulfillment delay, evidence consistency, causal remedy and financial loss | Existing evidence-backed remedy and reconciliation controls; uncertain cause remains unresolved |
| Outcome deadline/nonresponse | Follow-up effectiveness, customer-reported versus verified results | Existing consent-aware follow-up schedule; never fabricate agency outcome |
| Ad invoice / campaign threshold / daily close | Mature cohort CAC, contribution, budget, liability reserve | Pause a campaign within standing authority; increase/reallocate only with valid funded cap and evidence |
| New build or feature exposure | Regression against baseline and guardrails | Canary containment or rollback under release policy; engineering job with exact build evidence |
| Missing heartbeat/event/delivery | Watcher health, queue lag, capture coverage | Reconcile, retry bounded local work, escalate unresolved infrastructure failure |

Every AI decision has: decision_id, evidence references and as-of time, metric/schema/build/model versions, coverage, options, selected action, assumptions, uncertainty, expected benefit/downside, cost limit, authorizing policy, expiry, guardrails, actual execution receipt and outcome. Store this in an engineering/business decision journal downstream of authoritative events. AI hypotheses never become financial facts.

## D. Slack and optional analytics

| ID / state / gate | Scope and dependencies | Acceptance evidence |
| --- | --- | --- |
| S01 · BUILD · AUTO | Slack read-only observatory; T04/J07/M03. | One parent/thread mapping per journey, concise failure timeline, current repair state and cohort business summaries. Durable delivery/reconciliation handles rate limits and ambiguous responses. Closed fields only; no raw fact values, customer stories, access tokens or payment/provider IDs. Verify a finite test in the intended channel before calling it live. |
| S02 · BUILD · OPTIONAL | GA4 Measurement Protocol projection; M01/T04. | Separate analytics identity and permitted campaign dimensions; consent/retention controls; valid event names/types/session semantics. No fabricated engagement time or workspace token as client_id. Validation endpoint plus actual property receipt verified. A collection HTTP 2xx is not evidence that an event was processed. GA4 outage cannot block appeals. |
| S03 · QUALIFY · AUTO | End-to-end observatory receipts and access; S01. | Failed synthetic journey deep-link opens only to authorized operator; export lag and dead-letter work appear. Slack outage does not stop analysis/repair. Adding @ChatGPT is optional for core queue operation; its presence alone is not a persistent worker. No Slack write path can bypass existing controls. |

Incoming webhooks do not return a parent message timestamp. For a reliable per-journey thread, use an appropriately scoped Slack app/API or an explicit supported timestamp reconciliation path; do not pretend the original simple webhook helper implements threads. The original urllib helper is synchronous, best-effort and unqueued; it is not the durable implementation.

## E. Customer completion and service quality

| ID / state / gate | Scope and dependencies | Acceptance evidence |
| --- | --- | --- |
| P01 · QUALIFY · BETA | Required ticket image and OCR attempt, then manual/all-null/refused/corrected review through TicketFactReview and ticket_intake_service.py. Local attempt gate, recovery tests and joined paid OCR browser rehearsal implemented. Live qualification remains open. | New reviews/confirmations/submissions cannot skip OCR for the current photos or bypass a pending attempt. Customer-supplied correction after a completed OCR attempt reaches its intended path without unnecessary repeat model comparison. Literal evidence and adopted facts stay distinct. Edits invalidate stale confirmations. No O/0 guessing or blanket identifier canonicalization. Test current code before treating an old review claim as an unfixed bug. |
| P02 · EXTEND · BETA | Image intake/device handling, including large camera files and HEIC. | Supported formats decode safely, metadata is stripped, dimensions/size bounded, unsupported input gets an actionable retake/export path. No silent blank image. Actual iPhone/Safari and Android landscape/background/reload/offline checks on the candidate; keyboard/screen-reader and Rules modal behavior checked. |
| P03 · EXTEND · PAID | Controlled correction/restart and truthful stop semantics in LiveLetter/case service. A verified corrected remail stays RETURNED until fresh approval of its PDF and envelope. | Existing stop UI is clearly a local review pause. If server cancellation or destination revision is added, it has durable state, clears preview/signature/approval, revalidates official destination and requires renewed exact-packet approval. Never mutate a frozen packet or resubmit when earlier mail is uncertain. The initial mailing still needs a tested correction or assisted restart path before launch. |
| P04 · QUALIFY · BETA | Official procedure/research evidence and configured provider combination. | Production-aligned bounded OCR/research trial covers successful, uncertain, refused and customer-corrected inputs with evidence and cost receipts. Quote/source freshness and unsupported physical-original requirements tested. Validate real configured access and budgets; historical direct-reader diagnostics are not proof of the entire customer path. |
| P05 · QUALIFY · BETA | Customer access, email delivery and recovery. | Link scanner/prefetch GET does not accidentally consume consent or authorize a mutation; workspace access and expiry checked. Explicit customer action for destructive steps. Real bounded email receipt, bounce/retry, sender-domain configuration and recovery verified without leaking bearer links. |
| P06 · QUALIFY · PAID | Payment, print and mailing proof through existing controls. | Test-account identity/mode, signed webhook replay/order handling, exact PDF hashes, page geometry, return address and certified-mail output inspected. Hold deadline comes from provider evidence; review-window expiry is separate. No capture without existing authorized evidence trigger. No duplicate mail/capture under interruption/replay. Physical mail/live charges require their own defined authorization. |
| P07 · EXTEND · BETA | Remedy/support workflow and all-case outcomes; J05/J04. | Existing proven system-address errors receive the required full remedy. Non-address causal exclusions require evidence; unknown cause is not a denial. Routine triage can be automated; primary/backup responder and coverage exist for unresolved cases. Real alert/acknowledgment drill and support response ownership demonstrated. |
| P08 · QUALIFY · BETA | Trust/copy/pricing/terms consistency. | UI, email, Rules, guarantees, checkout and configured price/service phase agree. No unmeasured 95% reliability, success guarantee, guaranteed transit time or invented rejection statistics. Current clerical boundary remains clear. Material policy changes receive applicable owner/review authority and versioned consent; do not rewrite historical consent. |

## F. Deployment, continuity and release

| ID / state / gate | Scope and dependencies | Acceptance evidence |
| --- | --- | --- |
| R01 · EXTEND · BETA | Reproducible candidate and CI gates: Makefile, package locks, cloudbuild manifests. | Backend lint/format/types/tests, frontend lint/types/build, qualification self-tests and clean-checkout tooling checks pass for the recorded source. Existing Cloud Build manifests only build images; qualify actual CI/test/promotion configuration. Immutable image digests and build configuration identify deployable artifacts. |
| R02 · QUALIFY · BETA | The [November 18 target receipt](verification/r02-target-preflight-2026-11-18.json) records the signed isolated deployment, private TLS, authenticated ingress, a single attached writer, and a full stop and restart with data preserved. [Local cutover and gateway evidence](verification/r02-cutover-2026-11-18.json). | Verify the current candidate and approved cost cap, single writer/fencing, persistent regional storage, IAM, private backend TLS, public frontend origins, secret references and lifecycle supervision on the target. |
| R03 · QUALIFY · BETA | Backup/restore/reconciliation drill using existing recovery tools. | Off-host backup restores to a quarantined target; old owner fenced; original provider identities reconciled before unquarantine. Current NEEDS_EXTERNAL recovery state remains a blocker until required external evidence exists. Verify actual RPO/RTO and restore completeness, not only backup-file existence. |
| R04 · QUALIFY · BETA | Independent monitoring, scheduler and emergency controls. | Poll/sweep freshness and process identity checks run; total VM loss is detected outside that VM. Verify stop-new-paid, stop-free-beta and stop-all-new-intake separately while callbacks/recovery for existing obligations continue. Queue health, disk pressure, stale ownership and missed jobs tested. |
| R05 · EXTEND · BETA | Privacy, retention and access lifecycle; retention_inventory.py/retention_plan.py. | Journey links/events and downstream decision/Slack/GA4 copies are inventoried. Policy and legal/financial retention requirements are verified before activation; proposed retention is not an active purge. Governed expiry/erasure with audit evidence coexists with append-only business history and backups. Revocation, least privilege and leak checks cover diagnostic data. |
| R06 · QUALIFY · BETA | End-to-end deployed synthetic rehearsal; R01–R05/P01–P08. | Actual DNS/TLS/origins/network, configured modes, intake, recovery, notifications and restricted monitoring verified on exact candidate. Fake/test/live evidence clearly labeled. No critical unowned blocker. Staff and cost obligations documented. |
| R07 · DECIDE · BETA | Concrete paid-beta activation packet covering R06, PAID, the M02 ledger and reserves, and B01. The [cohort cap](operations/service-mode.md) is enforced at the first payment intent. Set its funded value on the qualified target at activation. Full M03 acquisition analytics can follow initial admission. | Price, customer disclosures, qualified correct-office/on-time promise and remedy, target/artifacts, funding/reserve ceiling, enforced paid-cohort cap and stop conditions are concrete and authorized. No free entitlement batch. Record activation and first paid fulfillment separately; update readiness from evidence. |
| R08 · QUALIFY · PAID | First paid-beta cohort reconciliation; R07. | Reconcile actual capture, fulfillment, remedies, costs and outcomes at the configured price before expanding the cohort. Preserve reserve, support-capacity and spend limits. Paid demand and mature contribution remain measured outcomes, not assumptions. |
| R09 · QUALIFY · AUTO | Production repair pilot; T08/R08. | One eligible real regression moves from event to independently verified, authorized staged release and measured outcome with rollback ready. No owner incident forwarding required. Record interventions and unresolved steps honestly; a successful local synthetic run alone does not close this gate. |

## G. Business operation and acquisition

| ID / state / gate | Scope and dependencies | Acceptance evidence |
| --- | --- | --- |
| B01 · DECIDE · PAID | Merchant identity, banking, bookkeeping, customer support ownership and business obligations. | Actual account identities, settlement destination, tax/accounting responsibilities, provider agreements and policy owners established. AI prepares calculations and forms; it cannot supply Amir's identity or invent authority. Cash commitments and recurring overhead feed M07. Verify current requirements with authoritative sources when executing this task. |
| B02 · BUILD · SCALE | Demand and acquisition experiment pipeline; M01–M07/R08. | Specific audience/problem, supported claim, CTA, campaign ID, budget, landing variant and success/stop metric per experiment. SEO/community/video/paid channels selected by measured return and execution cost. No mass posting or ad purchase without the corresponding action authority. |
| B03 · QUALIFY · SCALE | Price and conversion sensitivity using mature paid cohorts. | Actual supported price arms, costs, expiry/recovery and refund/remail rates feed scenario engine. Report ranges and sample limitations. Stop or revise losing tests within policy; increase spend only when conservative contribution, reserve and service-capacity limits permit. |
| B04 · BUILD · SCALE | Continuous operations cadence; M05/R04/J05. | Event hooks plus daily financial reconciliation and scheduled cohort review start automatically. Backlog prioritization balances customer harm, reliability, contribution and engineering cost. Tasks have owners, receipts and unresolved states. Weekly strategy output contains decisions/actions/outcomes rather than raw notifications. |

### Executable acquisition policy

The advertising system is an action loop, not a dashboard that waits for Amir to interpret it. Add these bounded implementation tasks to the operating queue:

| ID / state / gate | Scope and dependencies | Acceptance evidence |
| --- | --- | --- |
| B05 · BUILD · SCALE | AI creative production and experiment lifecycle; B02/M04/M05. | Generate copy, CTA and creative variants from versioned approved product facts, brand assets and channel constraints. Verify price, eligibility, service/guarantee claims and landing-page consistency before publication. Reject unsupported win rates, invented testimonials and legal promises. Assign creative/version/campaign IDs and track exposure through capture, refund and contribution. Pausing, replacing and retiring variants is automatic within standing authority. |
| B06 · BUILD · AUTO | Budget allocator and platform action adapter; B05/M03/M05/R08. | Policy tests prove aggregate account/campaign/day caps under concurrency, delayed reporting, provider timeout, duplicate job, external manual edit and restart. Read back remote state; uncertain mutation is reconciled before another mutation. No model can raise its own cap. A stale or incomplete ledger prevents budget increases. Dry-run shadow decisions are compared with actual outcomes before enabling spending. |

At each decision window, compute spendable cash after committed costs and required reserves. Remaining acquisition capacity is the minimum of the funded acquisition allocation, current period cap, platform/account cap and capacity-based limit, less spent and reserved exposure. Use consistent units and time windows. Reserve proposed spend atomically before issuing a platform change; reconcile actual charges and release unused reservation with evidence. Account for platform pacing/overshoot and reporting delay in the safety buffer; a nominal daily platform budget is not proof of an absolute spending ceiling.

For each eligible campaign, estimate mature contribution per acquired unit and its uncertainty from M03/M04. Existing observed conversion already includes review expiry; never subtract that loss twice. Choose among pause, hold, bounded exploration and bounded increase using the preregistered decision policy. New creatives may receive a small funded learning allocation despite sparse data; expansion requires adequate evidence. Guardrails override the optimizer if fulfillment, refund, support, cash or data-quality limits fail.

Optimize expected contribution subject to those constraints, rather than clicks or gross revenue. Maintain a control/holdout where feasible, cap each adjustment, and wait through the declared observation period before evaluating another change. A verified policy breach can trigger immediate containment; noisy early conversion is not a verified breach.

The initial channel shortlist from the business discussion is high-intent local search content, relevant community referrals and Hurtle-led short-form creative. These are test priorities, not proven winners. SEO/content labor and production costs count as acquisition costs; “organic” is not automatically free. Generic paid search receives no assumed favorable economics. Compare every channel using supported cohort contribution and measured execution cost. Channel-specific rules and current platform capabilities must be checked before implementing its adapter.

Once standing authority is configured, approved-fact creative variants and routine allocation changes do not require a separate human approval for each iteration. Material new product claims, changed guarantee/price authority, increased funded limits or new account access remain outside that scope.

## Authority boundaries

| Action | Autonomous when implemented and configured | Boundary |
| --- | --- | --- |
| Aggregate, reconstruct, diagnose, prioritize | Yes, using authorized restricted evidence | Preserve unknowns; AI output is not a business fact |
| Reproduce and patch isolated code | Yes, bounded worker/task/check policy | No production secrets, weakening trusted tests or hidden scope expansion |
| Resolve a known operational retry | Existing state-machine/idempotency rules | Unknown external side effect requires reconciliation first |
| Verify an agency destination | Gather official evidence automatically; apply only through validated existing authority | No inferred address, self-issued evidence, or approval bypass |
| Refund/remail | Existing evidence-backed policy and financial controls | No blanket denial; no new manual gate imposed on existing automatic remedy |
| Rollout/rollback | Within qualified standing release policy | Changing funding, guarantees or legal/product authority is not an ordinary patch |
| Marketing test/spend adjustment | Within funded campaign limits, maturity and guardrails | No unfunded or expired cap; no unsupported success claims |
| Account identity, new funding, material policy | Prepare a concrete decision packet | Owner or legally required authority remains necessary |

The goal is to remove human incident relay and routine calculation. It is not to remove accountability, fabricate evidence or grant an agent unlimited spending authority.

## Disposition of old advice and clutter

The former raw review dump contained useful risk prompts mixed with assumptions about older code. Its actionable themes are now assigned: manual/all-null and literal/adopted flow (P01), HEIC/camera behavior (P02), budget/expiry and pending-operation recovery (T03/J03/P06/R03), print geometry and physical-original limitations (P04/P06), email prefetch/access (P05), and single-writer deployment (R02).

Do not adopt its suggestions to log raw OCR text, normalize uncertain identifiers into guesses, infer calibrated accuracy from model scores, create another uncertainty ledger, or treat an old Cloud Run inventory as the current target. Verify reported defects against current code before making changes.

The stopped Figma handoff's product constraints survive in current product docs and P01–P08. Its runtime PIDs, old worktree state and “unverified latest edits” checkpoint are superseded. Current coding policy belongs in AGENTS.md. Historical files remain recoverable from Git; duplicating them in an archive folder would recreate the clutter.

Keep consolidation provenance, source-specific verification receipts, hash-addressed official-source fixtures and reference contracts used by tests. Four historical mobile rehearsal directories are summarized in docs/verification/browser-rehearsals-2026-11-16.json with original hashes, failed/interrupted results and Git recovery paths; the polish receipt is under docs/verification/polish-2026-11-16.json. Historical governance artifacts are not the live task queue. New generated run output belongs outside the source tree or in ignored locations; do not publish caches, browser recordings or stale handoff copies.

## Definition of ready to operate

The planned paid beta requires every BETA and applicable PAID admission gate with inspectable evidence and an authorized activation packet. Continued paid operation and expansion require reconciled first-cohort evidence. Unattended repair adds AUTO gates. Scaling adds SCALE gates and supported positive economics. Optional GA4, new mockups, storage rewrites and arbitrary model proliferation do not block those gates.

Completion means the service can fulfill safely, recover, account for money, observe customer outcomes, diagnose and repair bounded defects, and operate acquisition within measured economics and funded limits. Sustainable profit still has to be demonstrated by demand and mature paid cohorts; no amount of completed code guarantees it.

## External interface references

Recheck these primary interfaces when implementing; do not copy obsolete examples from chat.

- [GA4 Measurement Protocol reference](https://developers.google.com/analytics/devguides/collection/protocol/ga4/reference) and [validation](https://developers.google.com/analytics/devguides/collection/protocol/ga4/validating-events).
- [Slack incoming webhooks](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/) and [chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage/).
- [Stripe authorization and capture](https://docs.stripe.com/payments/place-a-hold-on-a-payment-method): use the actual capture deadline and eligible payment-method rules.
