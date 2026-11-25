# ParkingBreaker Execution Roadmap

Updated November 24, 2026 (Pacific). This plan orders engineering work. [Beta readiness](BETA-READINESS.md) owns the release decision. Paid activation is **NO-GO** and the free First-Class pilot is closed.

## Destination and next work

**The public launch is nationwide from day one.** Drivers across the U.S. can bring a fresh municipal parking citation for first-stage review. Identify the printed issuer and stage from the ticket, apply the existing service-policy holds, and offer paid Certified Mail only when the filing route, recipient, deadline and packet requirements are verified for that case. The same path must work for an unfamiliar issuer. The optional five-case First-Class pilot exercises the mailing operation at one office and does not set the public market boundary.

**Success for the driver** is completing a timely filing from a phone, reading and approving the exact packet, and getting an accurate mailing record. When we cannot verify a filing, the driver needs a timely explanation and a safe stop. **Success for the business** is repeat paid demand that leaves at least **$100 a month above operating break-even**, the owner's early profitability target. Plan within the $450 total budget. Record the remaining cash and authorized spend before each provider trial. A free mailing can teach us about routing and customer effort. Paid orders, settled funds and actual costs establish whether the business works.

### 1. Replay the customer path and its stop reason

Run `python3 build-kit/scripts/synthetic_rehearsal.py --timed-first-class` from the project root. The local replay sends two synthetic images through ticket reading and customer confirmation, then runs the normal issuer resolver and research worker. The supported case requires approval of the exact PDF and produces one fake First-Class mailing across two service restarts. The second case changes the wording above the same address to “Mail payments only to.” The saved finding identifies the payment wording and dispatch produces zero mailings. Both cases require zero payment calls. [Rehearsal runner](../build-kit/scripts/synthetic_rehearsal.py) · [Two-case rehearsal](../backend/tests/test_timed_first_class_rehearsal.py)

The receipt records the software fingerprint, source hashes, saved decision, observed milestones and elapsed times. The OCR provider is scripted and the simulated customer supplies the ticket fields. The next timing measurement belongs on the intended deployment with its actual providers.

### 2. Qualify one receiving office and run a bounded free pilot

Use the ticket back to lead the search. San Diego publishes the same box for Step 1 appeals and payments. Its older ticket names “Parking Administration – Citation Appeals.” Seek a current official posting or form that names the recipient and directs first-stage appeals there. The enrollment code requires both in the same source. If the office takes time to respond, use another issuer with complete evidence for the rehearsal. San Diego stays closed while its recipient is unresolved. [City appeal instructions](https://www.sandiego.gov/parking/citations/appeal) · [City payment instructions](https://www.sandiego.gov/parking/contact/parkingadmin) · [City ticket instructions](https://www.sandiego.gov/sites/default/files/legacy/parking/pdf/citation_instructions.pdf)

On November 24 the offline intake replay gave the issuer assessor Boston’s published appeal instructions and both Sacramento contest pages with search disabled. The earlier intake found none of those pages under the same conditions. The First-Class replay still stopped on both cities. Boston is the next case to trace through the full extractor. Its by-mail section places the appeal letter requirements and the mailing address in consecutive steps. Test that evidence alongside Sacramento’s separate payment box. Chicago’s seven-day issuance window and later 21-day notice window also need distinct ticket-stage and mail-timing evidence before enrollment. [Boston instructions](https://www.boston.gov/departments/parking-clerk/how-appeal-parking-ticket) · [Sacramento instructions](https://www.cityofsacramento.gov/public-works/parking/citation-services) · [Chicago hearing FAQ](https://parkingtickets.chicago.gov/EHearingWeb/FrequentlyAskedQuestions)

**How we calibrate a disputed rule.** A reviewer uses the confirmed ticket front and back to locate the issuing agency, the deciding office and the driver's filing stage. One model interprets the dated official passage; a second tests the interpretation against a similar instruction that would change the outcome. Use the same questions across jurisdictions: What does mailing accomplish? Who receives it for that purpose? What must be enclosed? Which mailing methods are allowed? Which event starts the clock, and what must happen by the deadline? Keep each agency's own wording with its answer. A fact missing from one source triggers a search for better evidence. A documented alternative, such as a legible copy, counts only where the procedure offers it.

For a shared mailbox, test two cases with the same address. The positive needs official first-stage filing instructions identifying the receiving office and box. Payment instructions or a later hearing's address cannot supply that evidence. Preserve competing readings, their cited passages and the check that settles the disagreement in the existing benchmark or verification receipt. Report separate counts of incorrect mail approvals, incorrect stops and cases with missing evidence, including results on other issuers held out from rule design. [Research questions](../backend/src/parkingbreaker/research/questions.py) · [Cross-jurisdiction answer key](bench/answer-key.jsonl)

**Next interpretation experiment.** Start with Cambridge's archived first-stage instructions. They recommend the web form and also tell the driver how and where to mail a dispute. In the existing research call, have the model identify each action, who takes it, the filing stage, and whether the method is required, offered, or preferred. Ask it to cite the supporting passage and flag contrary instructions or missing evidence. Use the same reading to distinguish a payment address from a filing address and a later hearing deposit from a first-stage requirement. An exact quote proves the words appeared on the page. Whether those words support the conclusion needs a separate check. [Cambridge instructions in the test archive](../backend/tests/fixtures/unfamiliar_issuers/cambridge.txt) · [Existing research extractor](../backend/src/parkingbreaker/research/extractor.py)

Run the first comparison offline using archived pages and independently recorded answers. Include Cambridge, an online-only issuer, a payment-only address, a later-stage requirement and a previously unseen issuer. Compare unsafe approvals, unnecessary refusals and correctly unresolved cases with the current checker. Measure time and model cost per case. Keep the experiment out of dispatch. If safe filing decisions improve on held-out cases, review the change to the live gate separately. Source identity, freshness, exact quotes, packet approval and dispatch authorization remain software checks.

**Code this route needs.** Commit `fc6fc03` handles San Diego's citation-accuracy questionnaire. The separate numbered-stage parser patches still need an owner freeze exception. A shared-box change must pass the exact confirmed recipient for this issuer and stage and refuse payment-only, wrong-issuer and wrong-stage evidence. Enrollment checks multiple sources, while the timer watches one URL. If the route relies on a second page, save and recheck that page too. Change the office or an essential mail rule after approval and verify zero new mail requests. A reported move can trigger immediate scoped revocation. [Route monitor](../backend/src/parkingbreaker/route_monitor.py) · [Office monitoring and revocation](operations/controlled-beta.md#monitoring-a-confirmed-mailing-office)

**Pilot decision.** After the authorized target rehearsal covers print, return handling, postal events, access, feedback and restart, the owner can open up to five free First-Class cases for one issuer and stage over 14 days. Count every selected case. Record assistance, wait, agency acknowledgment, returns and unresolved outcomes. An upheld ticket can still show that we reached the right office. Pause the affected route on a routing error and support its accepted cases. [Pilot procedure](operations/controlled-beta.md#ordinary-first-class-feedback-pilot)

### 3. Prove the paid Certified Mail path

Prepare this sandbox case while waiting for the agency. Earlier drills released test holds and rendered a Certified proof. The November 22 deployed case stopped in issuer research. The missing proof is one transaction tying the approved PDF and envelope to a test hold, qualifying Lob evidence, a Lob-signed callback and one Stripe capture. Interrupt between the provider response and the local write. Restore in quarantine and find the original objects. Pass with exactly one letter and one capture. [Qualification receipt](verification/p06-r05-2026-11-22.json) · [Recovery procedure](operations/controlled-beta.md#recovery-and-rollback)

**Code where the drill fails.** Repair the first broken step in issuer discovery, submission or payment recovery and retain its regression case. In parallel, qualify the physical-phone experience, delivered access email, privacy controls and acknowledged outage alert. Paid activation still requires a qualified shipping candidate, funded postage and refund reserves, staffed support, and the owner's signed cap, dates and stop rules.

### 4. Decide whether to repeat or expand

**Before increasing the cap, extend the existing paid report.** It counts local captures, refunds and open work. Join those records to actual fees, postage, remails and payout evidence. Verify the first cohort against statements and mark missing invoices as unknown. Start from gross captures, deduct refunds and costs once, and use the bank payout as a cross-check. Include acquisition, support and OCR/research for stopped intakes. Deduct monthly fixed costs once. Alongside surplus, report how many people started, qualified, saw an offer, approved and paid, and where others stopped. [Paid cohort report](../backend/src/parkingbreaker/paid_beta_report.py) · [Price funnel](../backend/src/parkingbreaker/price_funnel.py)

With positive measured contribution per paid order, monthly orders needed for the target are `ceil((monthly fixed costs + $100) / contribution per paid order)`. Compare that with actual order volume. With zero or negative contribution, work on price, cost or conversion before buying traffic.

**Decision after each cohort.** Hold increases to volume and spend while payouts or outcomes are unknown. Pause affected intake on a safety failure. If customers drop out or costs exceed revenue, fix the largest measured cause and repeat within the approved cap. Increase volume when repeat cohorts show demand, reliable fulfillment and enough margin and cash to fund the next cohort.

**Work discipline.** Keep one product change in progress. Each session ends with a receipt or a reproducible blocker and the decision it supports. Revisit bulk city preloading, more parser variants, dashboards and autonomous repair when real filings, support incidents or measured costs give them priority.

---

## 1. Non-Negotiable Product, Legal & Financial Rules

1. **Clerical boundary only (zero legal representation):** Customers supply and confirm their own facts, wording, and selected evidence. Service-generated text enforces `appeal/proofread.py` (`BANNED_PHRASES`)—never invent legal arguments, cite unverified statutes, assess win probability, or promise dismissal.
2. **Verified official destination or full stop:** Every accepted appeal requires an OCR-read ticket image attempt, customer-confirmed issuer, and verified official `.gov` agency evidence establishing the receiving office, mail-in eligibility, and filing deadline. Do not use a `.gov` suffix alone as proof that mail-in contesting is permitted; online-only, in-person-only, private-lot notices, wet-signature/notary/original-stub requirements, or unresolved essential facts must stop cleanly.
3. **Triple-gated deadline safety:** Feasibility (`latest_safe_send_date`) is checked at research completion, packet freeze, and the mail-create boundary, accounting for production business days, USPS Certified Mail transit, and conservative holiday/cutoff buffers. We never sell or dispatch an appeal that cannot meet the verified filing rule.
4. **Paid beta and a separate bounded mail pilot:** The paid launch keeps its Certified Mail and payment gates. A separately approved pilot may offer five no-charge ordinary First-Class cases in one qualified jurisdiction during a 14-day window. Keep pilot results separate from paid launch evidence. See [pilot operation and qualification](operations/controlled-beta.md#ordinary-first-class-feedback-pilot). For paid cases, keep four states distinct in code, tests, and receipts:
   - **No authorization:** Unsupported intake stops before placing a card hold where determinable upfront.
   - **Authorization placed (`requires_capture` with `amount_capturable > 0`):** A created `PaymentIntent` awaiting confirmation is not an authorization.
   - **Authorization cancelled/released:** Mandatory whenever post-hold research or timing proves infeasible, or review expires ($0 captured).
   - **Payment captured & mail dispatched:** Permitted only after exact PDF + envelope hash approval by the customer and qualifying provider submission evidence.
5. **Single durable authority:** Extend the existing single-writer SQLite transaction store, append-only `pb_case_history`, pure `oplog/appeal_events.py`, and evidence-backed `ledger.py`. Never run paid customer traffic on ephemeral container memory.
6. **Business record-keeping boundary:** Scope all ParkingBreaker ledgers, provider inventories, receipts, and cohort reports strictly on or after **2026-11-16**. Pre-launch activity in shared provider accounts is out of scope and excluded.

---

## 2. Engineering ownership and verification

- One writer owns each change. Use the owner's selected engineering tool for bounded work. The integrating engineer verifies the actual diff, runs the affected checks and records the result.
- Qualify production dispatch, follow-ups, backups and monitoring with the founder’s workstation offline.
- **Continuous evidence-bound execution:** Track work across five distinct states: **implemented -> tested -> configured -> live -> measured**. Reuse valid historical receipts when source digests, configuration, and acceptance criteria still match; re-run only checks invalidated by edits or required once on the frozen release candidate. Do not stop at status reports—complete all unblocked authorized work and surface a single concise request when owner input or spend approval is required.
- **Credential & secret safety:** Never commit, echo, pass in CLI `argv`, or log secret keys, bearer links, or raw customer content. Verify any previously exposed development credential (including MiMo Token Plan keys) is rotated and stored write-only. Inject live production credentials via Secret Manager references rather than plaintext environment variables.

---

## 3. Phase 1: Critical Path to Launch (`BETA` + `PAID` Gates)

Steps 1 through 7 are the detailed gates for paid activation. Reuse matching receipts for implemented items and close the remaining gaps on the shipping candidate. The free pilot has its own admission and rehearsal criteria above.

### Step 1 — Unify Worktrees & Freeze Release Candidate (`R01`)

| ID / Gate | Scope | Acceptance Criteria & Required Evidence |
| --- | --- | --- |
| **R01 · BETA** | Select one clean shipping commit on `main` and verify its CI and Cloud Build provenance. | 1. Select the reviewed changes needed for this candidate and record the clean commit. The decline-timestamp fix is already integrated.<br>2. Use the existing candidate-tagged Cloud Build recipes, record the immutable image digests and verify CI test and promotion steps for this commit. [Build receipt](verification/increment-2026-11-21a.json)<br>3. Pass `make check` (ruff lint, format, strict `mypy`, full disk-backed pytest suite + short-path socket fixture), `make web-check` (lint, `tsc`, production build — run sequentially, never concurrently), `make qualification-check`, and all three synthetic browser campaigns (`paid`, `paid` + `--ticket-reading-preview`, and `controlled_free_beta` `375x812` mobile OCR) sharing one `application_fingerprint`.<br>4. Record commit SHA, tree hash, test counts, and immutable image digests in `docs/verification/r01-candidate-YYYY-MM-DD.json`. |

### Step 2 — Verify the core customer and money paths (`P01`, `P03`, `J01–J05`, `R05`, `M02`)

| ID / Gate | Scope | Acceptance Criteria & Required Evidence |
| --- | --- | --- |
| **P01 · BETA** | Mandatory ticket image + OCR attempt gate and review provenance (`TicketFactReview`, `ticket_intake_service.py`). | Confirm reviews/submissions cannot bypass a completed OCR attempt for current photos; literal OCR output and customer-adopted corrections remain distinct; edits invalidate stale confirmations; zero `O`/`0` guessing. |
| **P03 · PAID** | Controlled correction, assisted restart, and truthful stop semantics (`LiveLetter`, `case_service.py`). | Verify local pause vs. server cancellation; ensure initial mailing has a tested correction or assisted restart path before launch; any destination or text change clears preview/signature/approval and requires fresh exact-packet PDF + envelope approval; returned mail stays `RETURNED` until re-approved. |
| **J01–J05 · BETA / PAID** | Core customer journey, intake, fulfillment, and outcome follow-up hooks (`page.tsx`, `ticket_ocr.py`, `case_service.py`). | Browser observations (`J02`, strict CSP, no PII/tokens/URLs) remain distinct from authoritative server milestones (`J01`); OCR/research pause reasons and budget basis recorded (`J03`); hold, release, approval, submission, scan, capture, return, refund, and remail states tracked with idempotency keys (`J04`); consent-aware outcome follow-up cadence verified under fake-clock tests (`J05`). |
| **R05 · BETA** | Privacy, data retention, and access lifecycle (`retention_inventory.py`, `retention_plan.py`). | Verify retention inventory covers all active tables (including support tables), bearer links/revocation, and backups; legal/financial retention rules verified; diagnostic bundles leak zero secrets or raw ticket PII. |
| **M02 · PAID** | Evidence-backed ledger cost & obligation ingestion (`ledger.py`, `paid_beta_report.py`). | Captures, refunds, disputes, processor fees, postage/remail obligations, and AI usage reservations distinguish `estimated`, `reserved`, and `billed` states; verify the local aggregate cohort report (`operations/controlled-beta.md`) runs cleanly. |

At the candidate freeze, review the integrated diff and receipts, resolve concrete failures and record the shipping commit.

### Step 3 — Durable Target Preflight & Qualification Deployment (`R02`)

| ID / Gate | Scope | Acceptance Criteria & Required Evidence |
| --- | --- | --- |
| **R02 · BETA** | Durable `cloudrun-private-vm` topology preflight and isolated target verification (replaces ephemeral Cloud Run `/data`). | 1. Run local preflight on the frozen SHA: `bash deploy/validate.sh --dry-run`, `PB_DEPLOY_TOPOLOGY=cloudrun-private-vm bash deploy/lifecycle.sh deploy --dry-run`, `rollback --dry-run`, and `pytest tests/test_t22_disk_policy.py tests/test_t22_cloudrun_overlay.py tests/test_t22_lifecycle.py tests/test_release_readiness.py`.<br>2. Price the bounded qualification drill window (Cloud Run containers, private VM `pb-qual-backend-01`, replicated regional disk `pb-qual-data-01`, backups, network, teardown steps, and residual post-shutdown costs; distinguish GCP billing alerts from hard quota/spend caps) and obtain Amir's approval.<br>3. Deploy candidate by immutable `sha256` digest to the isolated target using root-owned `0600` `/srv/parkingbreaker` env + Secret Manager refs (never plaintext keys). Prove single attached ext4 writer, old-owner fencing, data survival across full VM stop/restart, private TLS on TCP `8443`, public refusal of internal/worker routes, and fresh monitor poll/sweep. Write `docs/verification/r02-target-preflight-YYYY-MM-DD.json`. |

### Step 4 — Physical Phones, Live OCR, Official Procedure & Copy (`P02`, `P04`, `P05`, `P08`)

| ID / Gate | Scope | Acceptance Criteria & Required Evidence |
| --- | --- | --- |
| **P02 · BETA** | Physical mobile camera/HEIC intake and viewport checks. | Execute on **physical Android (Foxx A56 — budget prepaid, 2 MP/5 MP sensors; record the exact model)** and **physical iPhone (Safari)** against the deployed candidate (emulation does not satisfy `P02`): verify large camera/HEIC decode, EXIF stripping, landscape/reload/background survival, keyboard/screen-reader basics, and Rules modal. **Coverage note:** the Foxx A56 is a low-end camera device, so a pass is a *floor* (harsh OCR/legibility test), not representative of typical drivers; add one mid-range Android (e.g. Pixel A-series or Samsung A-series) to represent mainstream hardware, or record the limitation explicitly in the P02 receipt. |
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

After paid activation, recruit nationally through a channel that reaches drivers with current tickets. Apply the funded cohort cap across all issuers. Before opening, replay first-stage cases across multiple issuers, including an unfamiliar issuer with a verifiable route and an issuer where the service must refuse. During launch, track how many in-scope intakes reach a verified paid offer, broken down by issuer and reason for stopping. Include the cost of refused intakes when judging whether the channel pays for itself.

1. **Measure demand within the approved budget.** Begin with a small owner-approved outreach channel and record eligible drivers, offers, purchases, acquisition cost and reasons for stopping. Use existing first-party attribution. Any paid campaign requires its own approved channel, cash limit and stop rule. Review spendable cash and reserves before increasing exposure.
2. **First Paid-Cohort Reconciliation (`R08 · PAID`):**
   - Reconcile every Cohort #1 journey from photo start -> verified offer -> refusal/exit -> hold -> capture -> actual billed Lob Certified Mail invoice -> first Stripe bank payout settlement -> customer/agency outcome (dismissed, denied, clerical rejection, pending/unknown).
   - Calculate true net unit contribution including AI/OCR/support costs of non-converting stopped tickets before raising `PB_PAID_COHORT_CAP` or ad spend.

---

## 5. Phase 3: Post-Launch Backlog (`SCALE`, `AUTO` & `OPTIONAL`)

Review this backlog after the first paid cohort reconciliation, or earlier for a demonstrated operational gap. The separate business-day deadline change still needs evidence from an applicable official filing procedure. The [guard freeze](../build-kit/state/guard-freeze.json) records the earlier Cambridge exception.

### A. Autonomous Diagnosis & Bounded Repair (`AUTO`)

**Owner dashboard requirement (planned).** Record issues automatically. Show a count of new issues, group repeat occurrences, and let the owner click **Investigate with Astra 6.2** to send all new entries in view to GPT-6.2 Astra in one investigation. Entries arriving during the run stay new. Loading the dashboard only reads the log. A repeat click or worker restart resumes the same investigation. Show the resulting diagnosis and its evidence beside the entries. Any proposed code change goes through the existing test and release process.

Each entry should identify the time, operation, failed check, observed values safe to share, retries and outcome, including what happened to the letter and payment when known. Record the running build revision when the issue occurs. Link the exact file, function and line in that revision, attach references to the saved evidence and include the closest reproducible test. Mark missing build or evidence information as unknown. Send only the redacted diagnostic record to Astra. Its report should distinguish what triggered the stop, what caused it, and what evidence would settle an open question.

**Acceptance case.** A synthetic campus citation points at the city’s path on a shared portal. The saved research records a refusal, the case stops, and the hold is released. After a restart, the owner should see one issue. One click should let Astra reproduce the path mismatch, cite the failing check, and explain why the refusal was correct. The current research record already persists the fetch reason in `pb_intake_research.research_json`. The dashboard-to-agent handoff is pending. [Path check at the tested revision](https://github.com/venomason/ParkingBreaker/blob/4f825f2b314554709fc501c094879af50a126aaa/backend/src/parkingbreaker/research/sources.py#L171) · [Refusal at that revision](https://github.com/venomason/ParkingBreaker/blob/4f825f2b314554709fc501c094879af50a126aaa/backend/src/parkingbreaker/research/sources.py#L582) · [Regression case](../backend/tests/test_t21_issuer_scenarios.py)

- **T01 (TESTED):** Pure read-only `journey_timeline.py` reconstruction.
- **T02 (EXTEND):** Full build/config/exposure version provenance across all journey events (preserving `LIMITED` on legacy rows).
- **T03 (EXTEND):** Atomic attempt-transition history alongside mutable claim/authorization state updates.
- **T04 (BUILD):** Transactional SQLite outbox with per-consumer checkpoints and leases (zero HTTP inside write transactions).
- **T05–T07 (BUILD / QUALIFY):** Deterministic integrity scanner + deduplicated repair queue (`T05`), isolated-checkout patch worker with zero live secrets (`T06`), and synthetic fault-to-verified-patch proof (`T07`).
- **T08 & R09 (BUILD / QUALIFY):** Staged release coordinator (`T08`) and supervised production repair pilot (`R09`).
- **J06 & J07 (BUILD):** Telemetry gap/lag canary (`J06`) and authenticated operator timeline view (`J07`).
<!-- TODO(T05–T08): When an incident recurs after reconciliation, reproduce it from a redacted
     fixture in an isolated checkout. Require the regression to fail before the patch and pass
     afterward. Replay it twice and assert the expected letter and charge counts for that case.
     Feed results to the staged release gate and track recurrence by deployed revision. -->

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
| Local code, tests, dry-runs & read-only audits | Authorized in the active single-writer checkout | Preserve unrelated edits, assertions and trusted gates |
| Read-only provider & cloud inspection (`GET` / `describe`) | Permitted when scoped to $\ge \text{2026-11-16}$ | Never echo, log, or commit keys, raw PII, or out-of-scope pre-launch history |
| Cloud builds, VMs, disks & deployments | Requires explicit owner scope, window & dollar cap | Pin immutable `sha256` digests; verify live health and rollback spec |
| Refunds & remails on accepted cases | Existing deterministic evidence-backed controls | Never block a proven system-address-error refund behind manual chat approval |
| Live charges, physical mail, webhooks & ad spend | **Owner-authorized per action/cap only** | Presence of live keys in `.env` or Secret Manager is **not** spending or activation authority |
