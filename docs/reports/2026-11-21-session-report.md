# ParkingBreaker — session report (2026-11-20 → 2026-11-21)

Working session report: candidate reconciliation, live deployment, provider inventory,
launch-evidence reviews and activation preparation. Prepared for owner review; every claim
below is backed by a named artifact or receipt.

## 1. Outcome in one paragraph

The two divergent code lines were reconciled into one candidate, an independent frontier review
caught (and we fixed) a semantic defect in the merge resolution, the candidate passed lint,
format, strict types, 13,898 backend tests and three browser campaigns (224 checks), and it is
now **deployed and verified live at parkingbreaker.com** as a fake-provider preview revision.
The live Stripe and Lob accounts were inventoried read-only, the paid-beta activation path was
documented end-to-end, and one blocking finding was surfaced: the deployed target's storage is
ephemeral, which must be resolved before any real customer pays.

## 2. Candidate reconciliation (R01)

Two work lines shared base `f0a0bf8`:
- **Desktop worktree** — 35 uncommitted paths (support system, paid-beta reporting, pricing funnel)
- **Provisional line** (`0da1fe60`, `provisional-guarded-paid-fixtures`) — 3 commits, 22 files

Merge rehearsal (scratch only) produced exactly **2 conflicts** — `intake_store.py` and
`test_t14_authorization_recovery.py` — resolved by taking the provisional line's implementation+
test pair. Patch artifact: `r01-candidate-v2.patch` (988 lines,
`sha256 b72a797fad71258b05127f6939272b4849aa0b4a710c432c255dae38959040cf`), `git apply --check` clean,
and **applied to the shared worktree on 2026-11-21 under explicit owner instruction** ("commit
all changes and push"), superseding the earlier coordination hold.

### Review finding (external review, GPT-6.2 Astra via bounded packet)

The first merge resolution discarded corruption detection: the provisional SQL counted a
malformed decline timestamp as a recent decline (`COALESCE(julianday(...), 1)`), where the
desktop line raised `StorageCorruption`. Corrected rule, now implemented and regression-tested:
every in-scope decline timestamp must parse as timezone-aware, malformed rows raise
`StorageCorruption` before counting, and a valid recent decline cannot mask a corrupt row.

## 3. Verification evidence on the candidate

| Check | Result |
|---|---|
| `ruff check` | All checks passed |
| `ruff format --check` | 499 files already formatted |
| `mypy` (strict) | Success, no issues in 499 files |
| Full backend suite | **13,898 collected / 13,898 passed / 0 skipped / 0 failures** (exit 0) |
| Paid browser campaign | 155/155, report valid |
| Paid-OCR campaign | 14/14, valid |
| Mobile-OCR campaign (`chromium-mobile375x812`) | 55/55, valid |
| Qualification self-tests | routing + vision passed, `provider_called: false` |
| Shared fingerprint across campaigns | `2871be0a8e43…` (same recorded source) |

Campaign receipts: `~/tmp/pb-{paid,paid-ocr,mobile-ocr}-*/rehearsal.json`. Each carries its own
"Local synthetic rehearsal only" label.

## 4. Deployment (owner-authorized)

**"deploy to parkingbreaker.com"** (owner instruction) — executed 2026-11-21 07:07–07:26 UTC.

- Service `parkingbreaker-frontend` (Cloud Run, us-west1), revision **`parkingbreaker-frontend-00010-k6r`**, 100% traffic
- Images pinned by immutable digest: backend `b379bba0…`, frontend `b572e4d6…` (builds `dbb1f1a1…`, `1f91dba6…`, both SUCCESS)
- Build commit `ffd1e848…` echoed by `https://parkingbreaker.com/api/health` → **200, `status: ok`, `mode: fake`**
- Runtime preserved exactly: `PB_PROVIDER_MODE=fake`, `PB_SERVICE_PHASE=paid`, `maxScale=1`, `internal-and-cloud-load-balancing`, `invoker-iam-disabled`
- **Not paid activation** — no live providers, no charges, no mail

Two deployment incidents, both tooling errors on my side, both fixed and recorded in
`deploy-20261121T070749Z/receipt.json`:
1. `services replace` rejected a spec carrying the old revision name (`ALREADY_EXISTS`) — fixed by stripping the template revision name.
2. First applied revision served public **403** — the YAML render dropped service annotations including `invoker-iam-disabled`; restored `ingress` / `invoker-iam-disabled` / `maxScale` and verified the live spec equals the pre-deploy runtime settings.

## 5. Provider accounts (read-only inventory)

**Stripe live** (`acct_1UcRynQpJkemFkHB`, US/USD, company, created 2026-11-16):
charges+payouts enabled; 3 active one-time prices ($19.99 / $24.99 / $29.99);
1 product `parkingbreaker_certified_appeal`; **zero** payment intents, charges, refunds,
customers; **zero** webhook endpoints. Key verified working post-rotation (readback matched
the account in Stripe's activation email, per independent reconciliation).

**Lob live**: mode live; **0 webhooks**; 2 addresses; letters since business launch date
(2026-11-16): **0**. Per the owner's record-keeping rule (`~/Vault/Ops/policy/parkingbreaker-records.md`),
records are scoped to the business launch date and pre-launch transactions are excluded by policy.
Key is restricted (`/v1/accounts` 401): balance and billed packet cost are dashboard-only facts.

## 6. Launch analysis and activation preparation

- **Unit economics** (computed, assumptions flagged): contribution per appeal ≈ $9.19 / $14.05 / $18.90 at the three price arms (mail 822¢ estimate, Stripe fee 2.9%+30¢). Files: `~/Workspace/pb-coordination/unit-economics.{md,csv}`.
- **Fulfilment ceiling**: current Lob credit funds ≈ **6 baseline / 5 customer pieces** after one physical proof (independent arithmetic confirmed). Cohort cap rule: lower of owner cap and funded slots.
- **R07 activation packet (draft)**: `~/Workspace/pb-coordination/R07-activation-packet-DRAFT.md` — release identity, price arms, promise/remedy, staffing, cohort rule, stop authority; 8 owner decisions open.
- **Activation runbook**: `~/Workspace/pb-coordination/ACTIVATION-RUNBOOK.md` — exact env deltas (verified against `config.py`: live mode requires `sk_live_`/`live_` prefixes, refuses mixed keys), webhook provisioning calls for `https://parkingbreaker.com/webhooks/{stripe,lob}`, verified `gcloud` flags, rollback path.
- **🛑 GATE 0 (blocking for real money)**: the deployed revision's `PB_DATA_DIR=/data` is the 256 MiB **in-memory temporary volume** — a restart wipes every case, photo, approval and ledger row. Real paid cases require the R02 topology (private VM + regional disk) or explicit written acceptance of data loss (not recommended).

## 7. Trust/copy review (P08, deployed + source)

- Live copy: "Clerical service only; no legal advice and no promised outcome." No success/win rates, no guarantees, no attorney framing found in a targeted scan.
- Price agreement: journey shows "Early beta price: $24.99 · Certified Mail, everything included" — matches Stripe arm 2499; source tests cover all three arms.
- Trust badge visible at the commitment point: "NO CHARGE UNLESS THE LETTER IS MAILED"; checkout copy adds hold & refund terms and "No charge unless your letter is mailed."
- Email copy operational only; stop replies state "Nothing was charged. Your work is saved."
- **Structural enforcement**: `appeal/proofread.py` `BANNED_PHRASES` blocks guarantee/win-promise/attorney/strategy language in all service-created text (customer-literal exemption), so the clerical boundary is enforced in code. Evidence: `~/Workspace/pb-r01/p08-live-review/P08-review.md`.

## 7a. Provenance binding (verified after push)

The deployed images were built from tree `ffd1e848…`; a recursive source diff against the pushed
`main@5bf7921` found **zero code differences** (only generated caches, run output, and the
Next-generated `next-env.d.ts`). Chain: image digests `b379bba0…` / `b572e4d6…` ← source
`ffd1e848…` ≡ repo `main` code. `deploy/backup.sh --dry-run` also passes (restore
artifacts+records match, reconciliation `READY_FAKE`), which is R03's named first step.

## 8. Coordination and tooling

- A durable mailbox (`~/Workspace/pb-coordination/`) with the supervising agent (RED): 11
  outbox entries (changed gates, blockers, artifact paths only; no secrets), 3 inbox messages
  answered. RED owns Google Cloud build staging; provider work stays read-only until scoped.
- Frontier consulting wrapper (`~/bin/oracle`, logged at `~/.oracle/calls.jsonl`): 4 calls total
  this session; the design review is the one that changed an outcome (section 2).
- Deployment script + full log: `~/Workspace/pb-r01/deploy-preview.sh`, `deploy-20261121T070749Z/`.

## 9. Errors made and corrected (recorded, not buried)

1. First merge resolution kept both sides of a semantic conflict — tests rejected it; corrected to the coherent pair.
2. Patch snapshot initially missed 7 untracked files; `git apply` failure misread as clean due to a shell `&&` binding; rebuilt faithfully and re-verified.
3. Killed a 92%-complete suite run on a stale progress read (superseded tree).
4. `rsync --delete-excluded` destroyed a scratch clone's `.git` (throwaway dir; redone).
5. Deployment YAML render broke twice (old revision name; dropped service annotations) — both caught by verification, both fixed.
6. Suite summary line "missing" was a `-qq` display artifact; verified by two independent counts instead.
7. A GUI targeting mistake sent one coordination message into the wrong window; message lost, channel switched to the mailbox.

## 10. Open items

| Item | Owner |
|---|---|
| GATE 0 durability (R02 VM + regional disk) before paid cases | owner decision / GCP staging |
| GCP staging approval + image digests for the recorded build candidate | RED / owner |
| Webhook creation at `https://parkingbreaker.com/webhooks/*` (returns signing secrets) | needs owner go |
| R07 decisions: price arm, cohort cap value, reserve, B01 identity, responders | owner |
| Live-mail scope (one certified self-mail to measure actual billed cost) | owner |
| P04/P05 deployed joined journey (real OCR + access email) | needs live provider scope |
| Checkout payment-screen copy walkthrough (last P08 gap) | queued |

---
Artifacts referenced above live under `~/Workspace/pb-r01/` and `~/Workspace/pb-coordination/`;
this checkout's uncommitted verification receipts are included in the accompanying commits.
