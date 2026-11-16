# ParkingBreaker controlled-beta readiness — current handoff

**NO-GO for live beta.** Local source/smoke progress is verified, but this is not a
release or deployment approval. Customer intake remains clerical: readable ticket or
customer corrections; unresolved essential facts stop charge/dispatch. Do not guess
legal conclusions or introduce an issuer-by-issuer launch database or city allowlist.

## Source and publication

- Canonical writable checkout: `/home/ghostmonday/Desktop/ParkingBreaker-Codebase`.
- Public repository: https://github.com/venomason/ParkingBreaker ; default branch `main`.
- Consolidated parent: `e4e4a28a912c84c34e66044f9e47ae7aba3d6b4b`.
- The owner approved committing and pushing this revision on November 16. Publication
  targets `main`; the local review branch `beta/controlled-readiness-2026-11-15` is
  retained. The exact signed commit and remote verification belong in the publication
  receipt, discoverable through `~/Vault/Ops/runs/parkingbreaker-push-active.json`.
- Current candidate fingerprint: `20eed44a059e83b616bb741741c669f5e4cb08c462992fe3318b89c0e9cd8df4`.
- Latest paid-test fingerprint: `2f881ba92a1e182cb573a380c5d6727d36707c9ff6d330cac7842100e0314bea`.
- The parent's signature verifies locally; GitHub reports `unknown_key`. This revision
  requires an explicit signed commit and fast-forward publication with no history rewrite.

Consolidation history, recovery and legacy paths are recorded in
[CONSOLIDATION.md](CONSOLIDATION.md). No new legacy-folder moves occurred in this follow-up.

## Implemented and checked

The intake role now uses DeepSeek V4.1 Flash (`deepseek-flash`), including the shared
issuer/link-assessment client. Research stays on DeepSeek V4 Pro. Optional Gemini
packet-check configuration remains but is not constructed by the current customer-authored
service. The adapter supports validated inline JPEG/PNG, original detail, model-specific
image allocation, body limits and documented `max_tokens`; Pro rejects image input.
There is no silent provider fallback or automatic request retry.

The example selects fake providers and controlled free beta with explicit roles.
No credential file was copied into the checkout and no deployed service mode changed.
The inherited process DeepSeek key was rejected; the owner-supplied local reference
passed metadata/balance GETs and real inference. Brave access remains unqualified.

- November 16 publication checks: **13,643 backend tests passed**, with no failures
  and one dependency deprecation warning. Global Ruff lint/format, mypy across 476
  source files, and both offline qualification self-tests passed. See the
  [publication validation receipt](verification/publication-2026-11-16.json) for
  the tested input digest and commands. No paid provider call was made in these checks.
- This full run includes the latest prompt/intake changes and the repaired portable
  source-fixture loader. It supersedes the earlier full run with one fixture failure
  and the subsequent focused-only checks. The newest prompts remain unqualified live.
- Earlier frontend/release receipts remain source-specific evidence, not claims that
  every broad check was rerun on the newest candidate.
- Captured-provider replays confirm strict multiline refusal without repair and that
  the F comparison conflict never mutates the supplied assertion or becomes a match.

## Actual Android and camera

A56 / Android 13 / Chrome 157.0.8103.78 passed native front/back Files upload, cancel,
missing-fact blocking, confirmation/reload, edit invalidation, bad-file refusal,
injected connection failure/recovery and HOME/return. Actual camera capture of fictional
E, native gallery selection, JPEG upload (201) and reload also passed. Decoded pixels
and 1200x1600 dimensions are identical. Stored bytes match the existing metadata-stripping
filter; raw hashes are not identical. The camera photo contains monitor UI, stays private,
and was not sent to DeepSeek. These tests used fake providers and the recorded
`2270ee89...` application fingerprint. Subsequent source edits only change OCR prompts.

The localhost USB adapter does not qualify production TLS/DNS/origin security.
Landscape rotation and actual iPhone/Safari remain open. Owned test services, mappings
and tabs were stopped/closed; original phone files were retained.

## Real DeepSeek trials

First approved trial: three calls, stopped at B's fixture-title/stage error.
Second approved envelope: all 12 unique frozen requests completed; C extraction was not
retried. After its initial refusal, supervisor review allowed only the seven unused
independent requests to finish evidence collection. The original strict failed score
was retained. Across both envelopes, 15 unique calls used $0.51 in reservations and a
$0.15 conservative upward-rounded application usage estimate. This is not a provider
invoice or an inferred account debit. No more inference is running.

| Case | Extraction | Direct-reader comparison diagnostic |
| --- | --- | --- |
| A | Pass | Pass |
| B | Pass | Pass |
| C | Entire reply refused; original raw response not retained, cause unknown | Pass; plate unreadable |
| D | Refused: issuer value contains a newline | Model reads `DEM0926`; owner key says `DEMO926` |
| E | Pass | Pass |
| F | Citation correctly unreadable/null | Partial `TH-48271` returned as conflict in an extra probe |

**Scope matters:** the harness tested the adapter/reader directly. Production
`TicketFactBinding.review_fields()` returns only non-null literal fields, excluding
adopted-only values; `TicketIntakeCoordinator.request_review()` uses that method.
Actual F replay confirms its unreadable citation is omitted from the production
comparison payload. The extra unknown-citation assertion is not an ordinary production
request. C/D comparisons after refused extraction are likewise component diagnostics.
No wrong fact was shown to be automatically adopted into a production case.

Retain separate measures for field/format accuracy, correct safe refusal, customer
correction usability and fulfillment eligibility. The strict component run did not
fully pass; that does not establish that every negative-case refusal is a production
safety defect. Time, street location, violation code, fine and currency are not fields
in this reader's 11-field schema and were not qualified by these paid calls.

The latest prompts clarify single-line output and whole-field uncertainty, without
weakening the parser or forcing the expected O/0 value. These changes pass the focused
checks but are unqualified live. The unapproved four-call C/F probe plan is on hold
until the next test reflects production binding/coordinator behavior.

Detailed receipts: [corrected live assessment](verification/deepseek-corrected-live-2026-11-16.json),
[earlier live checkpoint](verification/deepseek-first-live-2026-11-15.json),
[Android smoke](verification/android-smoke-2026-11-15.json).
The current private operational report and Gemini CARBON review brief are discoverable
through `~/Vault/Ops/runs/parkingbreaker-current-handoff.json`.

## Go/no-go scorecard and next actions

| Gate | Status | Smallest next action |
| --- | --- | --- |
| Source/publication | Owner approved signed publication to main; beta remains NO-GO | Verify the publication receipt's commit against GitHub main; CARBON review remains a separate follow-up |
| Shell/browser | Local shell/Android route works; connector host unavailable | Use the working local route; connector repair is not a beta prerequisite |
| DeepSeek | Auth/funding and readable component examples pass; newest prompt/live customer path unqualified | Review actual null-field filtering, response contract and correction UX; prepare one finite production-aligned test |
| Brave | Runtime credential/access not qualified | Identify approved secure reference and verify access/limits before authorized search |
| 23 qualification rows | Unsigned; saved quote checks previously passed | Review retained uncertainties; never invent source facts or make these an issuer-lookup launch project |
| Target/security/recovery/intake stop | Local synthetic checks pass; target acceptance missing | Finalize named target/cost/IAM/TLS/storage/restore/stop proposal and exact provisioning/drill approval |
| Printing/webhooks | Local contracts pass; actual provider proof missing | Specific TEST-account proof/webhook approval and page inspection; no live letter |
| Alerts/human support | Local tests pass; real receipt and staffing unqualified | Name primary/backup/coverage; authorize a specific alert/acknowledgement drill |
| Physical devices | Exercised Android/camera paths pass; rotation/iPhone open | Complete actual landscape and iPhone/Safari handling on candidate |
| Release | NO-GO | Exact artifact, dates, cap and bounded entitlement batch after gates pass |

Historical cloud inventory is not a refreshed production-health claim. A GitHub push
is not deployment. No deployment, customer charge, real mail or outreach occurred.
