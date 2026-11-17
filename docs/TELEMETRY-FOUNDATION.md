# Telemetry Foundation (First Slice)

Implementation reference. Remaining work is tracked only in [ROADMAP.md](ROADMAP.md); release state is in [BETA-READINESS.md](BETA-READINESS.md).

This document describes the first durable slice of ParkingBreaker's journey telemetry ledger implemented in `parkingbreaker.journey_events` and wired into `SqliteTransactionStore` and `ticket_intake_store`.

## Tables and Hooks

Schema (`pb_store_versions` component `"journey_events"`, version `1`, initialized in `SqliteTransactionStore.__init__`):

- `pb_journey_links`: restricted mapping from `workspace_token` (PK) to opaque `journey_id` (`jrn_<32-hex>`) and `created_at` (UTC ISO-8601). Immutable via SQLite `BEFORE UPDATE` / `BEFORE DELETE` triggers.
- `pb_journey_events`: append-only ledger of `event_id` (`evt_<32-hex>`, PK), `journey_id`, `seq` (`>= 1`, unique per `journey_id`), `occurrence_sha256` (unique), `content_sha256`, closed `kind`, closed `outcome`, `schema_version`, `occurred_at`, `recorded_at`, `duration_ms`, `cost_cents`, and `cost_status`. Immutable via SQLite `BEFORE UPDATE` / `BEFORE DELETE` triggers.

Five atomic hooks call `append_journey_event_in_transaction`:

1. `complete_ticket_intake_operation` (`ticket_intake_store.py`, `IntakeOperation.EXTRACT` only): emits `ticket_read` (`succeeded`), `ticket_read_refused` (`refused`), or `ticket_read_unknown` (`unknown`). `REVIEW` does not emit an event.
2. `record_ticket_intake_confirmation` (`ticket_intake_store.py`): emits `facts_confirmed` (`succeeded`) per confirmed generation.
3. `record_intake_with_workspace_snapshot` (`intake_store.py`): emits `intake_submitted` (`succeeded`) on initial intake commit.
4. `complete_authorization_attempt` (`transaction_store.py`): emits `hold_authorized` (`succeeded`) for newly started paid holds (`PaymentState.NO_CHARGE` / `amount_cents == 0` beta cases do not emit `hold_authorized`).
5. `SqliteTransactionStore._apply_event` (`transaction_store.py`): emits `packet_approved` (`succeeded`) when a letter transition reaches `CaseState.FROZEN`.

## Same-Transaction Semantics and Failure Policy

- `append_journey_event_in_transaction` requires an already-active SQLite transaction (`database.in_transaction`) on the authority connection and executes before `COMMIT`.
- If the enclosing business transaction raises or rolls back, both the business mutation and the journey ledger write roll back together.
- **Failed transactions**: rollback leaves no `pb_journey_events` row behind. Rollback alone does not record failure; failed transaction evidence requires separate downstream or attempt-table accounting (e.g., `pb_ticket_intake_operations`, `pb_intake_authorization_attempts`, or outbox/attempt logs).

## Attempts Are Not Yet a Complete Event History

A refusal is a committed fact about an unsuccessful operation: `ticket_read_refused` and `ticket_read_unknown` already belong in this ledger. A rolled-back business mutation must not leave a success event. This does not make the journey stream the financial ledger.

`pb_ticket_intake_operations` commits a pending claim and later updates it with completion status. `pb_intake_authorization_attempts` also updates state/revision in place. These restricted tables contain fields that must never be exported wholesale, and their current rows do not preserve every intermediate transition or early validation failure. A background join cannot recreate facts that were never recorded. Roadmap T03 specifies durable transition evidence; T01 must expose incomplete capture rather than assert a complete history.

## Replay Policy

- Each emission derives `occurrence_sha256` from a domain-separated `occurrence_key` (`ticket_read:{request_id}`, `facts_confirmed:{intake_id}:{generation}`, `intake_submitted:{intake_id}`, `hold_authorized:{case_token}`, `packet_approved:{case_id}:{revision}:{attempt}`).
- `content_sha256` hashes `journey_id`, `kind`, `outcome`, `schema_version`, `occurred_at`, `duration_ms`, `cost_cents`, and `cost_status` (intentionally ignoring `recorded_at`).
- Exact replay (`occurrence_sha256` matches and both `journey_id` and `content_sha256` match) is a no-op returning the original `JourneyEvent`, preserving its original `event_id`, `seq`, and `recorded_at`.
- Same `occurrence_key` with differing `journey_id` or semantic content raises `JourneyEventConflict("journey_event_occurrence_conflict")`.

## Internal Restricted Links vs Safe Projection

- **Restricted internal linkage**: `pb_journey_links` maps `workspace_token` to `journey_id`, and `pb_intake_owners` maps `workspace_token` to `owner`. Both `pb_journey_links` and `pb_journey_events` are classified as `RESTRICTED_IDENTITY_OPERATIONAL` in `retention_inventory.py` and listed in `_RESTRICTED_SCOPES` in `retention_plan.py`.
- **Safe projection**: `JourneyEvent` and `JourneyEventPage` expose only `event_id`, `journey_id`, `seq`, `kind`, `outcome`, `schema_version`, `occurred_at`, `recorded_at`, `duration_ms`, `cost_cents`, and `cost_status`. They exclude `workspace_token`, `owner`, `intake_id`, `case_token`, provider IDs, `occurrence_sha256`, `content_sha256`, and free text.
- **Read boundary**: `read_journey_events` / `SqliteTransactionStore.journey_events` requires exactly one of `journey_id` or `workspace_token`, enforces bounded keyset pagination (`1 <= limit <= 100`, `after_seq >= 0`), and checks `pb_intake_owners` when `owner` is supplied (raising `JourneyEventAccessError("workspace_owner_mismatch")` on mismatch). This is internal pseudonymous/restricted data, **not** anonymous data, and no unauthenticated HTTP endpoint or external export exists.

## Clock and Cost Meaning

- `occurred_at`: UTC-normalized business event timestamp supplied by the transition/completion record, preserved unchanged.
- `recorded_at`: UTC insertion timestamp resolved once when a new row is written (defaulting to real `datetime.now(UTC)` in `append_journey_event_in_transaction`; injectable `clock` for tests). Delayed recording (`recorded_at > occurred_at`) and clock skew (`recorded_at < occurred_at`) are both permitted without clamping, timestamp fabrication, or rejecting valid transitions.
- `cost_cents` / `cost_status`: `known` requires an integer `cost_cents >= 0`; `unknown` and `not_applicable` require `cost_cents is None`. `extract_usage_cost` never invents `0` when usage JSON or `cost_cents` is absent, unparseable, or `ClaimStatus.UNKNOWN` (`cost_status="unknown"`, `cost_cents=None`).

## Authoritative Systems and Explicit First-Slice Limitations

- `pb_case_history` remains the authoritative post-case transition history. `oplog.appeal_events` derives lifecycle milestones from that history, and `ledger.py` derives money reports from recorded evidence. Existing business effects and operational counters keep their existing write paths; this journey stream does not decide financial or mailing transactions.
- **Explicit limitations of this first slice**:
  - Closed to **seven** event kinds (`ticket_read`, `ticket_read_refused`, `ticket_read_unknown`, `facts_confirmed`, `intake_submitted`, `hold_authorized`, `packet_approved`).
  - `journey_id` is allocated lazily at the first captured milestone for a workspace (no pre-milestone journey identity).
  - No build, attribution, or campaign dimensions yet.
  - No general start, pause, or retry coverage across arbitrary steps.
  - No historical backfill of unrecorded events. T01 now classifies gaps in the recorded evidence without inventing history.
  - No external exporter, delivery queue, telemetry analyzer, or autonomous repair loop.

## Supervisor Acceptance

- Coding worker: AGY `gemini-4-hydrogen-high`; supervisor independently reviewed the diff, ran checks, and applied one mechanical raw-regex lint correction in the test file.
- Accepted locally: 946 existing tests across 34 lifecycle, recovery, storage, payment, privacy, and retention modules; 10 new journey tests. The production-source hashes did not change between the 946-test run and acceptance.
- Ruff passed on all six changed source files and the new test module. Strict mypy passed on the same seven files. The regex-adjusted contract test was rerun and passed.
- No live provider calls, deployment, Slack/GA4 exporter activation, commits, or pushes were performed for this slice. Model throughput, billed cost, and productivity improvement have not been established by this run.
- Cleanup follow-up: Ruff formatting was corrected in `journey_events.py` and its test. All 10 journey tests, targeted Ruff lint/format and strict mypy on the seven touched files then passed. The earlier 946-test receipt remains evidence for its original source snapshot.
- T01 reconstruction is now implemented (see below). Next is T02 capture provenance/identity, followed by durable attempt history, delivery and the analysis/repair proof.

## OCR + manual review vertical slice (2026-11-17)

The new `ticket_observability` component (schema version 1) adds three append-only tables on the same SQLite authority connection. `pb_ticket_ocr_observations` commits with the extraction completion and its journey event. A failed completion transaction leaves the existing pending claim, never a fabricated success; replay never repeats the provider call. Legacy completions report missing coverage, not invented timings or backfilled decisions.

OCR evidence contains closed decision-reason counts, primary/escalation statuses, configured model names when available, and the effective routing policy version and thresholds. No field values, provider error text, images, or prompts are copied. Single-stage readers explicitly report unrouted suggestions; confidence thresholds are not retroactively attributed to them. Monotonic primary extraction, optional escalation, and coordinator spans are separate. Coordinator time encloses extraction/routing, excludes the final database commit, and overlaps child spans: do not add parent and child durations. Missing spans remain null. These are measured code boundaries, not invented network/queue subdivisions.

`pb_ticket_review_sessions` stores deterministic eligibility and pinned guidance versions; `pb_ticket_review_observations` stores bounded, deduplicated client observations. Owner-scoped API endpoints use the existing authentication/CSRF boundary. Session/observation nonces are 32 lowercase hex characters. There are at most 64 sessions per workspace and 64 observations per session. Exact retries return the original record; conflicting payloads are refused. Customer observations cannot create confirmations or milestones. Their explicit source is `client_observation`; even reported success is non-authoritative.

TicketFactReview records actual exposure separately from assignment, validation-failure counts, save/confirm attempts, and visibility checkpoints. Visible non-busy review, visible busy, and hidden time are disjoint elapsed intervals, measured with performance.now. Each observation drains its interval, so repeated checkpoints do not double count. Hidden time takes precedence over busy time. No timing is claimed to measure attention. Delivery retries the same ID/body at most twice, with request timeouts; missing delivery, pre-mount activity, final unreported intervals, and browser termination remain unknown. There is no guarantee of complete client capture or inferred abandonment.

The only canary treatment is an additional manual-entry guidance paragraph. It does not change required fields, saved edits, discrepancy choices, accuracy acknowledgment, OCR policy, destination evidence, frozen packet hashes, prices, financial actions, or mail authorization. If a late assignment arrives after editing/focus begins, baseline guidance stays visible and the observation records that actual exposure. React workspace remounts isolate sessions; sessionStorage preserves the session nonce over reloads when available.

Configuration defaults: `PB_REVIEW_GUIDANCE_CANARY_POLICY=baseline`, `PB_REVIEW_GUIDANCE_CANARY_PERCENT=0`, `PB_REVIEW_GUIDANCE_CANARY_VERSION=review-guidance-v1`. To stage a local canary, choose `canary`, a bounded percentage, and a new version. To roll back new-session allocation, use `baseline` with a new version and restart the backend. Existing sessions retain their assigned version, including after reload; a new tab/session or new workspace gets fresh allocation. This is a versioned configuration rollback, not an automatic statistical rollout controller.

All linkage remains restricted internal data. The owner API includes operation/session IDs and must not be exported wholesale to Slack/GA4. The new tables are included in retention inventory and restricted scopes. This slice neither configures nor activates external analytics, Slack delivery, autonomous repair, production deployment, or paid traffic.

Regression evidence includes a genuinely frozen paid-case fixture: snapshot every business table, allocate canary, write/replay observations, roll back new sessions, then compare every business row and the case status exactly. The exact-packet browser journey also exercises real durable observation delivery with synthetic providers. Browser canary rendering probes explicitly stub only assignment/delivery; durable policy, replay, access and rollback tests exercise the real backend.

Verification: [OCR/manual-review receipt](verification/ocr-manual-review-2026-11-17.json) records 13,722 passing backend tests, 250 passing integrated acceptance checks, strict mypy across 483 files, Ruff lint/format, frontend type-check/lint, three timing/retry tests, and 53 production-build synthetic browser checks (zero skips/flakes). The integrated application fingerprint matches the rehearsed source. These are local tests; no production activation occurred.

## Journey reconstruction and paid-intake OCR gate (2026-11-17)

T01 is locally implemented in `journey_timeline.py`, exposed only through the internal
`SqliteTransactionStore.journey_timeline(workspace_token=..., owner=...)` read surface.
It reads one bounded SQLite snapshot and makes no business writes. The pure
`reconstruct_journey` function combines committed milestones, server OCR decisions,
server review assignments and explicitly labeled client observations. It never
turns browser-reported confirmation into a committed milestone.

Ordering preserves ledger sequence and each review session's own sequence; the
source lanes merge by receipt time with deterministic ID tie-breaking. This is not
a claim of cross-source causality. Occurrence and receipt timestamps remain distinct.
The default late-record threshold is 60 seconds, configurable for pure reconstruction;
clock reversal is surfaced rather than corrected. OCR parent/child durations remain
separate, and client visible/busy/hidden intervals retain their non-attention label.
No absent occurrence timestamp is invented for client reports.

Exact repeated input is deduplicated; conflicting IDs/sequences or mixed journeys
are refused. Pagination uses at most 100 entries and binds the cursor to the immutable
projection fingerprint. A changed snapshot rejects the old cursor. The store caps
snapshot inputs at 10,000 rows and refuses oversized inputs instead of diagnosing a
silently truncated history. Consumers retain the returned snapshot while paging.

Predecessor checks distinguish absent, late, partial, legacy and legitimately optional
evidence. Confirmation routes come from the exact saved confirmation generation,
never a browser flag; funding comes from the case record. Historical no-charge
commitments do not require a paid hold. Customer correction does not fabricate
successful OCR. Pending or uninstrumented OCR operations appear as explicit coverage
counts, not invented historical transitions. No page or quiet interval proves
abandonment.

Version-one storage lacks independently recorded capture-start provenance, so the
store always reports LIMITED coverage. COMPLETE or LEGACY classifications in the pure
function require independently established provenance. Same-attempt causal linkage,
complete operation transitions and durable delivery remain T02/T03/T04 work. This
projection does not recover overwritten attempts, establish revenue, or start an analyzer.

Following November 17 owner direction, the launch template selects fake providers with
the existing paid service flow. Beta is a maturity phase, not a free offer. The new
intake gate requires a completed OCR attempt for the exact current photos before new
review, confirmation or submission. Missing and pending attempts stop progress.
Refused/unknown outcomes remain failures and can lead to explicit customer correction;
they do not independently verify essential facts, destination, deadline or safe mailing.
Existing confirmation/submission replay and saved-case mailing revalidation retain
their original commitment semantics.

Verification is recorded in [the T01 receipt](verification/journey-reconstruction-2026-11-17.json):
410 targeted regression tests passed; strict mypy and Ruff lint/format passed across
485 Python files; frontend type-check and lint passed. Frozen paid and no-charge case
snapshots remained unchanged by timeline reads. An additional run failed with temporary
SQLite I/O errors; after removing only this task's temporary databases, the same expanded
suite passed on fresh storage. The failed run is recorded rather than counted as success.
No live providers, deployment, paid activation or new guarantee publication occurred.
