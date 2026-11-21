# ParkingBreaker Execution Roadmap

Updated November 21, 2026 (Pacific). This is the authoritative execution plan for completing software qualification, activating the live paid service, and operating the first customer cohort. Product boundaries remain governed by [Customer Choice Rules](CUSTOMER-CHOICE-RULES.md) and [OCR routing](OCR-ROUTING.md); release evidence lives in [beta readiness](BETA-READINESS.md) and `docs/verification/`.

**Current release state: NO-GO for live paid customer activation.**
A fake-provider preview is deployed at [parkingbreaker.com](https://parkingbreaker.com), but its Cloud Run `/data` volume is ephemeral in-memory storage and its providers are set to `fake`. Live customer charging, physical Certified Mail dispatch, and paid advertising remain unauthorized until every Phase 1 (`BETA` and `PAID`) gate below has inspectable evidence on the exact shipping commit and an owner-signed `R07` activation packet.

---

## 1. Non-Negotiable Product, Legal & Financial Rules

1. **Clerical boundary only (zero legal representation):** Customers supply and confirm their own facts, wording, and selected evidence. Service-generated text enforces `appeal/proofread.py` (`BANNED_PHRASES`)—never invent legal arguments, cite unverified statutes, assess win probability, or promise dismissal.
2. **Verified official destination or full stop:** Every accepted appeal requires an OCR-read ticket image attempt, customer-confirmed issuer, and verified official `.gov` agency evidence establishing the receiving office, mail-in eligibility, and filing deadline. Do not use a `.gov` suffix alone as proof that mail-in contesting is permitted; online-only, in-person-only, private-lot notices, wet-signature/notary/original-stub requirements, or unresolved essential facts must stop cleanly.
3. **Triple-gated deadline safety:** Feasibility (`latest_safe_send_date`) is checked at research completion, packet freeze, and the mail-create boundary, accounting for production business days, USPS Certified Mail transit, and conservative holiday/cutoff buffers. We never sell or dispatch an appeal that cannot meet the verified filing rule.
4. **Paid beta only (strict payment-state precision):** Beta is a paid maturity phase, not a free giveaway; `controlled_free_beta` remains a regression/pause fixture only. Keep four states strictly distinct in code, tests, and receipts:
   - **No authorization:** Unsupported intake stops before placing a card hold where determinable upfront.
   - **Authorization placed (`requires_capture` with `amount_capturable > 0`):** A created `PaymentIntent` awaiting confirmation is not an authorization.
   - **Authorization cancelled/released:** Mandatory whenever post-hold research or timing proves infeasible, or review expires ($0 captured).
   - **Payment captured & mail dispatched:** Permitted only after exact PDF + envelope hash approval by the customer and qualifying provider submission evidence.
5. **Single durable authority:** Extend the existing single-writer SQLite transaction store, append-only `pb_case_history`, pure `oplog/appeal_events.py`, and evidence-backed `ledger.py`. Never run paid customer traffic on ephemeral container memory.
6. **Business record-keeping boundary:** Scope all ParkingBreaker ledgers, provider inventories, receipts, and cohort reports strictly on or after **2026-11-16**. Pre-launch activity in shared provider accounts is out of scope and excluded.

---

## 2. DSH Operating Model & Single-Writer Protocol

- **DSH-only architecture:** Execute engineering and qualification strictly through DSH plus human owner (Amir) participation where required. Do not depend on OpenClaw or any external autonomous agent daemon for launch or production operations. Production workers, follow-ups, backups, and monitoring must run independently of an open DSH session.
- **Keep one writer on this checkout; another model reviews after implementation stops.**
- **Writer & reviewer roles:** Detect active locks, background test runs, or concurrent checkouts before editing. Use the configured primary coding model (`deepseek-flash` at `max` effort where supported) for implementation and test execution. Once a candidate freezes, run a separate read-only audit pass (`mimo-v2.6-pro` in an isolated read-only context, or `mimo-v2.6-flash` for bounded log triage) inspecting actual `git diff`, failure paths, and `docs/verification/` receipts—never rubber-stamping a summary.
- **Continuous evidence-bound execution:** Track work across five distinct states: **implemented -> tested -> configured -> live -> measured**. Reuse valid historical receipts when source digests, configuration, and acceptance criteria still match; re-run only checks invalidated by edits or required once on the frozen release candidate. Do not stop at status reports—complete all unblocked authorized work and surface a single concise request when owner input or spend approval is required.
- **Credential & secret safety:** Never commit, echo, pass in CLI `argv`, or log secret keys, bearer links, or raw customer content. Verify any previously exposed development credential (including MiMo Token Plan keys) is rotated and stored write-only. Inject live production credentials via Secret Manager references rather than plaintext environment variables.

---

## 3. Phase 1: Critical Path to Launch (`BETA` + `PAID` Gates)

Launch requires closing **Steps 1 through 7** below. (`AUTO`, `SCALE`, and `OPTIONAL` tasks in Phase 3 do **not** block launch.)

### Step 1 — Unify Worktrees & Freeze Release Candidate (`R01`)

| ID / Gate | Scope | Acceptance Criteria & Required Evidence |
| --- | --- | --- |
| **R01 · BETA** | Reconcile working tree changes and scratch patches into one clean, committed candidate on `main`; verify CI and Cloud Build provenance. | 1. Reconcile uncommitted worktree changes and reviewed fixes (including strict `StorageCorruption` validation on malformed/tz-less decline timestamps in `intake_store.py`) into a clean commit; keep `output/`, temp DBs, and local artifacts out of Git.<br>2. Update or parameterize `cloudbuild.yaml` and `cloudbuild-backend.yaml` so builds produce candidate-specific, immutable `sha256` image digests rather than relying on mutable `:staging` tags.<br>3. Pass `make check` (ruff lint, format, strict `mypy`, full disk-backed pytest suite + short-path socket fixture), `make web-check` (lint, `tsc`, production build — run sequentially, never concurrently), `make qualification-check`, and all three synthetic browser campaigns (`paid`, `paid` + `--ticket-reading-preview`, and `controlled_free_beta` `375x812` mobile OCR) sharing one `application_fingerprint`.<br>4. Record commit SHA, tree hash, test counts, and immutable image digests in `docs/verification/r01-candidate-YYYY-MM-DD.json`. |

### Step 2 — Complete Remaining Launch-Blocking Code Paths (`P01`, `P03`, `J01–J05`, `R05`, `M02`)

| ID / Gate | Scope | Acceptance Criteria & Required Evidence |
| --- | --- | --- |
| **P01 · BETA** | Mandatory ticket image + OCR attempt gate and review provenance (`TicketFactReview`, `ticket_intake_service.py`). | Confirm reviews/submissions cannot bypass a completed OCR attempt for current photos; literal OCR output and customer-adopted corrections remain distinct; edits invalidate stale confirmations; zero `O`/`0` guessing. |
| **P03 · PAID** | Controlled correction, assisted restart, and truthful stop semantics (`LiveLetter`, `case_service.py`). | Verify local pause vs. server cancellation; ensure initial mailing has a tested correction or assisted restart path before launch; any destination or text change clears preview/signature/approval and requires fresh exact-packet PDF + envelope approval; returned mail stays `RETURNED` until re-approved. |
| **J01–J05 · BETA / PAID** | Core customer journey, intake, fulfillment, and outcome follow-up hooks (`page.tsx`, `ticket_ocr.py`, `case_service.py`). | Browser observations (`J02`, strict CSP, no PII/tokens/URLs) remain distinct from authoritative server milestones (`J01`); OCR/research pause reasons and budget basis recorded (`J03`); hold, release, approval, submission, scan, capture, return, refund, and remail states tracked with idempotency keys (`J04`); consent-aware outcome follow-up cadence verified under fake-clock tests (`J05`). |
| **R05 · BETA** | Privacy, data retention, and access lifecycle (`retention_inventory.py`, `retention_plan.py`). | Verify retention inventory covers all active tables (including support tables), bearer links/revocation, and backups; legal/financial retention rules verified; diagnostic bundles leak zero secrets or raw ticket PII. |
| **M02 · PAID** | Evidence-backed ledger cost & obligation ingestion (`ledger.py`, `paid_beta_report.py`). | Captures, refunds, disputes, processor fees, postage/remail obligations, and AI usage reservations distinguish `estimated`, `reserved`, and `billed` states; verify the local aggregate cohort report (`operations/controlled-beta.md`) runs cleanly. |

*Checkpoint:* Freeze the candidate commit after Step 1 & Step 2 pass, run the **read-only MiMo Pro review** against the diff and receipts, fix any concrete findings, and lock the shipping SHA.

### Step 3 — Durable Target Preflight & Qualification Deployment (`R02`)

| ID / Gate | Scope | Acceptance Criteria & Required Evidence |
| --- | --- | --- |
| **R02 · BETA** | Durable `cloudrun-private-vm` topology preflight and isolated target verification (replaces ephemeral Cloud Run `/data`). | 1. Run local preflight on the frozen SHA: `bash deploy/validate.sh --dry-run`, `PB_DEPLOY_TOPOLOGY=cloudrun-private-vm bash deploy/lifecycle.sh deploy --dry-run`, `rollback --dry-run`, and `pytest tests/test_t22_disk_policy.py tests/test_t22_cloudrun_overlay.py tests/test_t22_lifecycle.py tests/test_release_readiness.py`.<br>2. Price the bounded qualification drill window (Cloud Run containers, private VM `pb-qual-backend-01`, replicated regional disk `pb-qual-data-01`, backups, network, teardown steps, and residual post-shutdown costs; distinguish GCP billing alerts from hard quota/spend caps) and obtain Amir's approval.<br>3. Deploy candidate by immutable `sha256` digest to the isolated target using root-owned `0600` `/srv/parkingbreaker` env + Secret Manager refs (never plaintext keys). Prove single attached ext4 writer, old-owner fencing, data survival across full VM stop/restart, private TLS on TCP `8443`, public refusal of internal/worker routes, and fresh monitor poll/sweep. Write `docs/verification/r02-target-preflight-YYYY-MM-DD.json`. |

### Step 4 — Physical Phones, Live OCR, Official Procedure & Copy (`P02`, `P04`, `P05`, `P08`)

| ID / Gate | Scope | Acceptance Criteria & Required Evidence |
| --- | --- | --- |
| **P02 · BETA** | Physical mobile camera/HEIC intake and viewport checks. | Execute on **physical Android (Samsung A56)** and **physical iPhone (Safari)** against the deployed candidate (emulation does not satisfy `P02`): verify large camera/HEIC decode, EXIF stripping, landscape/reload/background survival, keyboard/screen-reader basics, and Rules modal. |
| **P04 · BETA** | Deployed live OCR + official `.gov` research trial. | With bounded provider caps, run four cases through the deployed intake/research path: (1) clear ticket reaching review with verified `.gov` office, mail rule, and deadline quote; (2) ambiguous identifier staying explicit; (3) unsupported/online-only/unverified issuer stopping cleanly with **zero hold and zero mail**; (4) driver correction after OCR preserving literal vs. adopted provenance. |
| **P05 · BETA** | Customer email access, prefetch safety, and recovery. | Verify real Resend delivery from `appeals@parkingbreaker.com` (SPF/DKIM aligned), confirm link-scanner `GET` prefetch cannot consume consent or mutate state, and verify 15-minute link expiry/regeneration without leaking bearer tokens. |
| **P08 · BETA** | End-to-end trust, pricing, and disclosure consistency. | Verify landing page, Rules modal, Step 4 checkout offer, confirmation/stop emails, and configured price arms agree verbatim on clerical scope, no-outcome-guarantee, and hold/refund terms. |

### Step 5 — Joined Payment, Certified Print & Crash-Recovery Drill (`P06`, `R03`)

| ID / Gate | Scope | Acceptance Criteria & Required Evidence |
| --- | --- | --- |
| **P06 · PAID** | Joined Stripe + Lob qualification case, proof inspection, and signed webhook replay. | Using owner-approved test/qualification credentials on the deployed target:<br>1. Confirm manual authorization (`status=requires_capture` and `amount_capturable > 0`), hold expiry vs. review window, and price assignment.<br>2. Persist pre-submission idempotency reference + frozen PDF/envelope SHA-256 *before* provider call; visually inspect every page of the rendered US Letter PDF and Certified Mail envelope proof (geometry, legibility, return/destination addresses, certified banner).<br>3. Deliver valid and tampered callbacks to `/webhooks/stripe` and `/webhooks/lob` (verify `400 invalid_signature` on tampered body, deduplication on replay, and uncaptured hold when only digital creation proof exists).<br>4. Interrupt once at the provider-response / local-commit boundary for `R03`. |
| **R03 · BETA** | Off-host encrypted backup, quarantined restore, and external reconciliation (`NEEDS_EXTERNAL` closure). | Run `bash deploy/backup.sh --dry-run`, then execute scheduled encrypted off-host backup of the interrupted `P06` state. Fence old owner, restore into a fresh target with `RESTORE_QUARANTINE.json` active, and reconcile pending payment/mail effects using *only* pre-interruption persisted references against the real Stripe/Lob objects (proving RPO $\le 60\text{ min}$, RTO $\le 120\text{ min}$, zero duplicate charges, and zero duplicate letters). |

### Step 6 — Emergency Controls, Responder Alert & Full Rehearsal (`R04`, `P07`, `R06`)

| ID / Gate | Scope | Acceptance Criteria & Required Evidence |
| --- | --- | --- |
| **R04 · BETA** | Independent outage probe and intake kill-switches. | Verify external probe detects full VM loss and delivers alert within $5\text{ minutes}$. Separately exercise `stop-new-paid`, `stop-free-beta`, and `stop-all-new-intake` (including under concurrent checkout retries for `PB_PAID_COHORT_CAP`), proving zero new provider side effects while callbacks and recovery for accepted cases continue working. |
| **P07 · BETA** | Support workflow, automatic remedies, and human responder acknowledgment. | Verify proven system-address errors trigger full automatic remedy without manual delay; confirm named human responder receives and acknowledges the `R04` test alert with recorded timestamp; verify support intake credentials and case-ID routing before exposing customer support codes. |
| **R06 · BETA** | End-to-end deployed rehearsal sign-off. | Reconcile all `R01–R05` and `P01–P08` receipts on the exact shipping SHA and durable target; confirm DNS, TLS, origins, monitoring, and rollback readiness with zero critical unowned blockers. |

### Step 7 — Owner Decision Packet & Live Paid Activation (`B01`, `R07`)

| ID / Gate | Scope | Required Concrete Outputs |
| --- | --- | --- |
| **B01 · PAID** | Merchant, banking, tax, and support ownership (Amir). | Confirm live Stripe settlement bank destination (`acct_1UcRynQpJkemFkHB`), Lob account funding/auto-pay state, bookkeeping/tax owner, and primary + backup support responder schedule. |
| **R07 · BETA / PAID** | One-page Paid-Beta Activation Packet & live cutover. | Compile strictly from verified receipts:<br>1. Shipping commit SHA + immutable backend/frontend `sha256` digests on durable `cloudrun-private-vm` production.<br>2. Live price configuration ($19.99 / $24.99 / $29.99 arms or single selected price) and versioned customer disclosures.<br>3. Enforced `PB_PAID_COHORT_CAP` set to the lower of owner cap and funded slots:<br>$$\text{Funded Slots} = \left\lfloor \frac{\text{Spendable Lob Balance}}{\text{Verified Billed Certified Packet Cost}} \right\rfloor - 1\text{ proof piece} - \text{Remedy Reserve}$$<br>*(Note: postage credit is not cash refund reserve; fund cash reserve separately and top up Lob credit before raising `PB_PAID_COHORT_CAP`.)*<br>4. Provision live `/webhooks/stripe` and `/webhooks/lob` signing secrets in Secret Manager, apply live env (`PB_PROVIDER_MODE=live`, `PB_ALLOW_LIVE=1`), verify `/api/health` reports `mode: "live"`, and update `docs/BETA-READINESS.md` from **NO-GO** to **LIVE PAID BETA (CAPPED)**. |

---

## 4. Phase 2: Initial Business Launch & First Cohort (`R08` & GTM)

Execute immediately after Step 7 (`R07`) is live and verified:

1. **Compliant Gig-Driver Paid Acquisition Test (Pre-`SCALE` Bounded Learning):**
   - **Target:** Mobile drivers in `r/uberdrivers`, `r/lyftdrivers`, `r/doordash_drivers`, `r/InstacartShoppers`, `r/AmazonFlexDrivers`, plus high-intent keyword threads (`"parking ticket"`, `"loading zone ticket"`, `"citation"`). Disable Reddit Automated Targeting Expansion.
   - **Compliant Clerical Creative:** Lead with time/hassle savings and verified `.gov` Certified Mail fulfillment—never "dismissal angles," legal strategy, or guaranteed wins. State clearly: clerical preparation + USPS Certified Mail at the live price, with $0 charged if an official timely mail-in path cannot be verified.
   - **Zero-CSP-Risk Attribution:** Launch Cohort #1 using registered first-party UTM parameters without adding third-party ad pixels that weaken frontend CSP.
   - **Budget & Auto-Pause Discipline:** Account for Reddit Ads' up-to-$20\%$ daily overdelivery allowance when setting daily caps (e.g., a $\$25/\text{day}$ setting requires $\$30/\text{day}$ funded headroom). Pause campaigns immediately when `PB_PAID_COHORT_CAP` reaches $80\%$ capacity or if any `R04` stop triggers.
2. **First Paid-Cohort Reconciliation (`R08 · PAID`):**
   - Reconcile every Cohort #1 journey from photo start -> verified offer -> refusal/exit -> hold -> capture -> actual billed Lob Certified Mail invoice -> first Stripe bank payout settlement -> customer/agency outcome (dismissed, denied, clerical rejection, pending/unknown).
   - Calculate true net unit contribution including AI/OCR/support costs of non-converting stopped tickets before raising `PB_PAID_COHORT_CAP` or ad spend.

---

## 5. Phase 3: Post-Launch Backlog (`SCALE`, `AUTO` & `OPTIONAL`)

Queued behind `R08` Cohort #1 reconciliation unless a specific item is needed earlier to resolve an observed operational gap:

### A. Autonomous Diagnosis & Bounded Repair (`AUTO`)
- **T01 (TESTED):** Pure read-only `journey_timeline.py` reconstruction.
- **T02 (EXTEND):** Full build/config/exposure version provenance across all journey events (preserving `LIMITED` on legacy rows).
- **T03 (EXTEND):** Atomic attempt-transition history alongside mutable claim/authorization state updates.
- **T04 (BUILD):** Transactional SQLite outbox with per-consumer checkpoints and leases (zero HTTP inside write transactions).
- **T05–T07 (BUILD / QUALIFY):** Deterministic integrity scanner + deduplicated repair queue (`T05`), isolated-checkout patch worker with zero live secrets (`T06`), and synthetic fault-to-verified-patch proof (`T07`).
- **T08 & R09 (BUILD / QUALIFY):** Staged release coordinator (`T08`) and supervised production repair pilot (`R09`).
- **J06 & J07 (BUILD):** Telemetry gap/lag canary (`J06`) and authenticated operator timeline view (`J07`).

### B. Algorithmic Unit Economics, Attribution & Scaling (`SCALE` & `OPTIONAL`)
- **M01 (BUILD · SCALE):** Controlled first-party campaign/UTM attribution and immutable offer-arm persistence.
- **M03 & M04 (BUILD · SCALE):** Pure metric engine (`CAC`, `Allowable CAC`, `Pre/Post-Acquisition Contribution`, `Review Expiry`, `Cash Runway` — never double-counting review expiry or booking counterfactuals as invoices) and preregistered experiment evaluator (`M04`).
- **M05–M07 (BUILD / QUALIFY):** Watcher-to-bounded-decision journal (`M05`), model cost/accuracy benchmark (`M06`), and cash/reserve runway view (`M07`).
- **B02–B06 (BUILD / QUALIFY):** Multi-channel experiment pipeline (`B02`), mature cohort price-arm sensitivity (`B03`), automated daily reconciliation cadence (`B04`), compliant creative generator (`B05`), and capped ad-platform budget adapter (`B06`).
- **S01–S03 (`AUTO` / `OPTIONAL`):** Scoped Slack thread observatory (`S01`, `S03`) and optional isolated GA4 Measurement Protocol projection (`S02`).

---

## 6. Standing Authority Boundaries

| Action Category | Permitted Execution | Hard Boundary |
| --- | --- | --- |
| Local code, tests, dry-runs & read-only audits | Autonomous in DSH single-writer checkout | Preserve unrelated edits; never weaken assertions or trusted gates |
| Read-only provider & cloud inspection (`GET` / `describe`) | Permitted when scoped to $\ge \text{2026-11-16}$ | Never echo, log, or commit keys, raw PII, or out-of-scope pre-launch history |
| Cloud builds, VMs, disks & deployments | Requires explicit owner scope, window & dollar cap | Pin immutable `sha256` digests; verify live health and rollback spec |
| Refunds & remails on accepted cases | Existing deterministic evidence-backed controls | Never block a proven system-address-error refund behind manual chat approval |
| Live charges, physical mail, webhooks & ad spend | **Owner-authorized per action/cap only** | Presence of live keys in `.env` or Secret Manager is **not** spending or activation authority |
