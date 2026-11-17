# ParkingBreaker customer API contract (fake preview)

Historical contract reference retained for regression tests. Current work and authority are in [ROADMAP.md](../../../docs/ROADMAP.md), [BETA-READINESS.md](../../../docs/BETA-READINESS.md) and [AGENTS.md](../../../AGENTS.md). The original dated decisions below are not a fresh release approval. References to companion historical documents absent from this checkout are archival names, not working links.

This is the task 1.1/1.12 skeleton, with task 1.13 route shapes. All services are
in-memory fixtures: no research, external authentication, uploads, payments, mail,
notifications or persistence. Restarting the process loses everything. Never use real
customer data or provider credentials here. The canonical machines, window and text
adoption helpers are reused; this preview does not implement production coordinators,
the Tier 1 evidence validator, packet freezing or the full submit gate.

## Request size boundary

The server rejects oversized bodies before JSON or webhook processing: 1 MiB for
ordinary requests, 13.5 MB for photo-upload JSON, and 4 KiB for access requests.
Limits apply to streamed bytes as well as Content-Length. Oversize returns 413 with
sanitized `request_too_large`; malformed/duplicate length headers return 400.

## Persistent CaseService (Lane C C6b)

Injecting `CaseService` uses the same routes with saved intake, research, draft and
adoption records. Worker commands are `research` and `draft`; replays do not redraft an
already saved letter. The default application still uses the preview fake below.

The Draft response adds `spans` (id, text, author `ai` or `customer`, support, paragraph, source lineage)
and `flags` (span_id, kind, details). Status exposes the same data as `draft_spans` and
`draft_flags`. Empty lists are omitted, preserving the preview transcript. Review flags
persist across restarts and block adoption until the customer edits the affected text.
Draft edits preserve exact submitted bytes, clear adoption, and retain workspace edits.

`POST /cases/{case_id}/packet-preview` requires the adopted current hash and optional
owner-held `signature_ref`. The durable service uses the same assembly and approval gate
as final approval, returning `pdf_base64`, `packet_sha256`, `envelope_sha256`,
`letter_text_sha256`, page count and the mailing/return addresses. Preview allocates no
submission identity, freezes no draft, prepares no request and calls no provider.

Final `POST /cases/{case_id}/approval` requires the adopted hash, both inspected packet
and envelope hashes, and the same optional signature reference. It rebuilds through the
shared path and rejects a mismatch before reserving/preparing the immutable request.
The existing transaction guards bind current research at reservation and preparation,
including crash recovery. Frozen retries compare the original prepared bytes/envelope
and retain the same submission token. The endpoint prepares, but does not send mail.

Customer photo pages follow the letter; applicable verified completed agency forms are
validated one-to-one. Required declaration wording must be evidenced and signature ink
comes from the owner's uploaded artifact. T12 remains unaccepted: citation-back/runtime
form inclusion, complete enclosure disclosure and every-page identification still need
final integration. The in-memory scripted preview refuses actual packet preview with
`packet_preview_requires_durable_service`; its synthetic approval hashes and mail events
below do not qualify this durable contract or any production behavior.

Stable 409 keys include `stale_draft`, `draft_needs_review`, `adopt_current_text_first`,
`citation_photo_required`, `evidence_photo_required`, `packet_not_ready`,
`approval_refused`, `case_not_approvable`, `frozen_packet_mismatch`,
`packet_preview_mismatch` and `research_revision_stale`.

## Durable dispatch and webhooks (Lane C C7)

Worker command `dispatch` submits the frozen approved request through the existing
submission coordinator. `POST /internal/dispatch` handles all cases, including window
expiry without a customer read. Both worker routes require the injected shared-secret
bearer (`worker_secret` in `create_app`; `fake-worker` is the preview default).
The response is `{"processed": N, "failed": F}`: `processed` is the number of due
coordinator calls and completed outbox acknowledgements, not the number of letters, and
`failed` is the number of cases that failed this sweep (counts only; one failing case never
stops the others). Each invocation runs at most one
submission and one payment call per case. Repeat scheduler calls perform original-object
inspection, then capture or release, and honor persisted backoff. Live database ownership
is never stolen; process exit releases the lock and cursors recover unfinished calls.

Payment wording: the card is authorized before research and charged **after the mail
provider accepts the letter**. A pre-mail stop or window expiry queues hold release.
An ambiguous Lob result retains the hold, retries the same key and frozen bytes, then
uses the existing metadata lookup branch near the idempotency-window cutoff.
No capture occurs until acceptance is committed. A free re-mail uses attempt 2's key
and emits no additional capture. Payment links have no delivery adapter yet and remain
pending in the existing outbox; log effects are acknowledged against the durable record.

`POST /webhooks/stripe` verifies `Stripe-Signature` with the Stripe SDK inside its adapter.
`POST /webhooks/lob` verifies `Lob-Signature` as HMAC-SHA256 over
`Lob-Signature-Timestamp + "." + raw body`, per
[Lob's webhook contract](https://help.lob.com/print-and-mail/getting-data-and-results/using-webhooks).
Both use a five-minute timestamp tolerance and injected secrets on `CaseServiceDeps`;
these modules do not read environment variables. Invalid signatures return 400
`invalid_signature`. Verified deliveries return `{"received": true, "duplicate": false}`;
a repeated provider/event id returns `duplicate: true`, including after restart.
Verified early Lob events return 200 only after a reduced authenticated envelope is
committed to the versioned SQLite webhook backlog, keyed by the letter id and submission
token it carries (indexed columns, filled at insert). Duplicate early deliveries retain
the first envelope. Replay goes through the same webhook logic, outside coordinator
transactions, and never scans the backlog or the cases: each sweep runs one pass over at
most 100 parked events, oldest first, and each case's dispatch replays only the parked
events whose letter id or token that case holds. A committed cursor binds by exact letter
id or a unique frozen submission token when its letter id is unknown. While a cursor has
no letter id, two parked letters carrying its token are ambiguous: neither binds, both
stay parked and the hold is kept. A letter carrying a token whose attempt already holds a
different letter (arriving later or parked earlier) is a conflicting letter: it is never
bound and moves to `pb_webhook_unmatched` with reason `CONFLICTING_LETTER`. A return
without metadata waits for independent acceptance before applying its exact-id return.
Backlog removal, bound deduplication, acceptance/capture effect and any return
transition share one transaction. Failure rolls back the binding and leaves the earlier
create commit and backlog intact. A replay row that conflicts (`webhook_busy`, or a lock
held elsewhere) is skipped and stays parked; it never aborts dispatch or turns a committed
webhook into a 409.

Every parked event reaches a final outcome. It binds and leaves the backlog, or it moves
to `pb_webhook_unmatched`, which keeps the reduced envelope, a reason code and the times
(the newest 5000 rows only). Reasons: `UNMATCHED_EXPIRED` (no case claimed it within 30
days of receipt), `CONTRADICTS_CASE_STATE` (its letter id or token belongs to an attempt
whose cursor was canceled or refused, for example a scan after a confirmed-cancel
release), `CONFLICTING_LETTER`, and `CANCEL_CONFIRMED` (Lob's own failed/deleted notice
for the exact letter this system canceled). Each move logs once, with opaque ids and the
reason only: WARNING for the first three, INFO for `CANCEL_CONFIRMED`. A moved event
never binds later, and its re-delivery returns `duplicate: true`. There is no staff
review flow.
Unknown Stripe references still return 409 `webhook_case_not_ready`; ownership/revision
conflicts return 409 `webhook_busy`. These retries require T26 provider qualification.
Stored records omit raw customer/provider payloads. Stripe notifications trigger reconciliation through the
existing payment coordinator, which retrieves original-object evidence before recording
payment events; the scheduler continues that work if webhook processing crashes.

Tracking returns stored Lob events and the current attempt's certified tracking number
with `fake: false`. Acceptance and USPS tracking statuses are not stored delivery proof.
Confirmed `letter.returned_to_sender` and `letter.certified.returned_to_sender` events
apply the existing return transition ([Lob event names](https://docs.lob.com/));
late events from prior attempts never move the current attempt backwards.
The preview transcript below documents unsigned webhook refusals; signed success/replay
and durable dispatch behavior are proved in `test_effect_dispatch.py`.

T17 piece B supervisory rulings (2026-10-22, architecture-v8 §20.19): failure events
use exact-letter/submission-token checks; mismatches are logged and ignored as
evidence. A later exact-letter USPS scan wins over a stored failure; uncertain
identity keeps the hold. Cancellation refusal alone never establishes production.
A first return scan commits acceptance and the return transition together, even
after authorization expiry, without extending payment deadlines. Stored scan
type/name/USPS time/details are retained per attempt from signed events and lookup
acceptance. `GET /cases/{c}/tracking` is unchanged (each signed event still appears once, as
"not stored delivery proof"); the retained scans stay in the store projection
`usps_scans` for a future delivery report, which is not yet shown. Missing scan times
stay absent, never invented.

A free re-mail requires original payment `CAPTURED`, including at dispatch. Pending
capture waits through the existing scheduler; released/written-off originals close
as returned with no charge, no re-mail and no refund. A local unpaid re-mail refusal
never claims a provider rejection. Signed returns for already accepted letters keep
the existing exact-id path when metadata is absent; explicit conflicting metadata
is ignored as return evidence.

### Returned-mail remedy status (T30)

The scheduler resolves each `RETURNED` case in isolation. A new frozen request atomically
binds its exact append-only destination-decision revision, owner and request fingerprint.
Responsibility always uses that revision, including its provenance and consent identity;
later verified corrections cannot reclassify the returned letter. Existing frozen requests
are never backfilled with a decision or a customer-directed exclusion.

`CaseStatus.return_remedy` is omitted until a remedy is durably applied, except that
a returned attempt 2 exposes `RETURN_PENDING` while its own remedy is unresolved.
That pending status uses attempt 2, `remail: false`, no refund claim, and reports whether
mailing cost evidence is still awaited. It contains
`attempt` (the returned attempt), `kind` (`FREE_REMAIL`, `FULL_REFUND`, `PARTIAL_REFUND`,
`PROOF_PACKET`, `ADDRESS_EXCLUSION` or `RETURN_PENDING`), a fixed `reason_code`, fixed plain `message`,
`refund_cents`, `refund_status` (`pending`, `completed` or null), `remail`, and an optional
`proof_packet`. Refund completion is reported only after original-charge reconciliation;
a requested or refused refund never claims settlement. Returned cases never use the
pre-mail stop's “not charged” copy.

A system-verified destination proved wrong receives the exact captured amount back even
when a correction is available; any additional of our faults never drops that refund for a
re-mail. Proven sole customer-address fault retains only the narrow recorded exclusion.
Unknown causes keep the existing remedy: one free re-mail if feasible, otherwise the price
minus the actual mailing cost when authoritative, otherwise the bounded estimated cost
described below. Every free re-mail requires a persisted new
`SYSTEM_VERIFIED` destination different from the returned address and re-renders the packet
with that corrected destination. Only the third blank-line-separated section emitted by
`render_letter` can change, and it must equal the original destination block exactly.
Every other adopted byte (including quoted addresses), attachment and signature pixel
is preserved. A mismatched destination section or unrecoverable signed artwork prevents
re-mailing and resolves as our service failure: one full refund through the existing
atomic remedy/refund path, no second letter. That fallback keeps the return's own
responsibility classification next to the service failure, so an agency-side case keeps
its `proof_packet` alongside the full refund.
Remedy storage, the attempt-2 frozen request or refund intent, and
`RemailRequested`/`ReturnClosed` commit together. The existing dispatcher owns all provider
calls and original-charge refund recovery. There is no additional capture or third letter.
A free re-mail that cannot be completed (provider rejection, our own address-gate
refusal, or an uncertain submission later resolved as not sent) also resolves to one
full refund of the captured amount through the same refund-intent path, exactly once;
the existing return-remedy scheduler requests it only after the original capture is
confirmed and committed. Submission-outcome and capture-completion transactions never
request this refund. A restart resumes the same durable refund intent.
Our own dispatch refusals are recorded as `SERVICE_FAILURE`, other completed non-sends
as `REMAIL_NOT_SENT`. An uncertain submission stays open until it resolves and never
closes early. The message and `remail` flag report attempt 2's actual outcome, with
fixed copy for a local address-gate refusal, provider rejection, non-send/cancellation,
uncertainty, in-transit mailing, delivery proof, or a second return. A local gate refusal
never blames the provider; returned/delivered/unsent re-mails set `remail: false` and
never claim an arrangement is still pending. With payment `CAPTURED` or `REFUNDED`, a
failed free re-mail's message states what happened and that the full refund was issued
or is pending; its `kind` is `FULL_REFUND` with `refund_cents` equal to the captured amount.
For any other payment state it says the letter was returned and no charge was made,
with `kind: RETURN_PENDING`, `refund_cents: 0` and `refund_status: null`.

An agency-side remedy exposes the actual mailed PDF (`pdf_base64`, `pdf_sha256`), letter id,
tracking number and stored event/time pairs in `proof_packet`; these records do not claim
delivery or an agency decision. It may also arrange the single free re-mail when feasible,
and the proof packet is kept when that re-mail later resolves to a refund.

The downstream application evidence seam reads saved `failure_facts` (or the return cause
the signed webhook ingress derived) and the recorded `mailing_cost_cents` only for the
exact returned attempt/letter. This does not qualify a new Lob payload contract: typed
findings are never invented from provider text. The signed return webhook's USPS return
reason is classified with the existing returned-mail rules and the reason/classification
are persisted with the webhook record; a bare return defaults to unknown cause, never sole
customer fault. The mailing cost is authoritative only when recorded as actual; otherwise
the documented expected cost for the mail class is used as a flagged estimate after a
bounded wait of 14 days from the first durable return transition for that attempt,
using our persisted ingestion time. Later notifications and provider timestamps cannot
extend that deadline. The cost basis is saved atomically with the resolution: all
estimated-deduction copy says "estimated mailing cost", including requested refunds,
confirmed refunds and zero balances. Authoritative actual deductions keep "actual
mailing cost". Older resolutions whose basis was not saved say "recorded mailing cost"
rather than infer it; late evidence cannot relabel a resolved deduction. Missing or
invalid cost is never treated as zero, and a full refund never depends on cost at all.
Tracking events with unsupported or missing `type` are ignored when deriving a return
reason; they cannot change UNKNOWN cause to agency fault.

## Customer-directed destination (T30)

When the official appeal mailing address cannot be verified from official sources, the
customer may supply an address and explicitly accept a specific disclosure; otherwise the
case stops with no charge and the hold is released. The persistent `CaseService` extends
its existing research and drafting steps; there is no second pipeline. The scripted preview
serves the real disclosure text but answers the three choice routes with 409
`destination_choice_requires_durable_service`.

Status gains an optional `destination_choice` (omitted otherwise):
`{required: true, reason_code, disclosure_version, disclosure_sha256, address_revision,
address_fingerprint}`, where the last two name the customer's latest saved address (null
until one is saved). While it is present the case waits inside the existing 30-minute
window; no paid drafting call happens and window expiry stops the case with the hold
released. Verification is never inferred from an AI summary, and an unverified address is
never relabelled agency-verified.

| Method | Path | Request model | Response model | Purpose |
|---|---|---|---|---|
| GET | `/cases/{c}/destination/disclosure` | none | DestinationDisclosure | Versioned owner-approved `v1` text and the sha256 of its exact UTF-8 bytes |
| POST | `/cases/{c}/destination/address` | DestinationAddress | DestinationAddressReceipt | Save a complete US address as a new revision (same address again is a no-op) |
| POST | `/cases/{c}/destination/consent` | DestinationConsent | DestinationConsentReceipt | Accept the disclosure for one saved revision and fingerprint |
| POST | `/cases/{c}/destination/decline` | none | CaseStatus | Stop with no charge and release the hold |

`accepted` in DestinationConsent has no default and must be the JSON boolean `true`;
anything else is refused (422 `invalid_request` for a missing or non-boolean value, 409
`destination_consent_not_accepted` for `false`). Consent is scoped to the case, its owner,
the exact address revision and fingerprint, and the current disclosure hash, using the
shared `address_policy` decision. A later address revision invalidates it; consent is then
required again before drafting or mailing, and the final Lob boundary re-checks the exact
outgoing address. Repeating the identical consent returns the same receipt. Addresses and
consent are accepted only while no draft exists; decline remains available until approval.
Stable 409 keys: `case_not_started`, `destination_choice_not_open`,
`destination_choice_not_required`, `destination_locked_after_draft`,
`destination_address_incomplete`, `destination_address_is_payment_address` (the address matches a
payment address in the case's official research; nothing is saved and the choice stays open),
`destination_address_required`,
`destination_address_stale`, `destination_fingerprint_mismatch`,
`destination_disclosure_stale`, `destination_consent_not_accepted`,
`destination_consent_refused`, `destination_decision_stale` and `case_stopped`.
The disclosure wording is the owner-approved v1 text (2026-10-21; architecture-v8 20.21).

## Local preview

Run from `backend/`:

```sh
uv run uvicorn parkingbreaker.api.app:app --port 8000
```

Interactive schemas: `http://127.0.0.1:8000/docs`; OpenAPI JSON:
`http://127.0.0.1:8000/openapi.json`. Dependencies were resolved with `UV_OFFLINE=1`.
All request/response models live in `src/parkingbreaker/api/schemas.py`.

## Authentication and assumptions

- `/health` is public. Every workspace/case route requires an
  `Authorization: Bearer fake-alice` or `Authorization: Bearer fake-bob` header.
  Contact email is customer data, never an ownership credential. Unknown or malformed
  tokens receive 401 with `WWW-Authenticate: Bearer`.
- `BearerVerifier.verify(token)` is an injectable protocol returning an authenticated
  `Customer` or `None`. The fixed fake tokens are only for local preview. Firebase email
  link issuance and verification are deferred. Assumption for this skeleton: pre-sign-in
  intake stays in the browser; authenticated server workspace creation occurs at contact,
  before any pre-hold work. No anonymous server workspace/claim protocol is introduced.
- Workspace and case ownership are enforced server-side on every customer route.
  A different customer's resource and an unknown resource both receive 404 with
  `{"detail":"not_found"}`. Worker routes accept only `Bearer fake-worker`, which
  is rejected by customer routes. Production worker OIDC is deferred.
- Money is integer USD cents. Timestamps are UTC ISO 8601; dates are `YYYY-MM-DD`.
  Enum strings are uppercase. Unknown request fields are rejected. Photo references
  are opaque fixture references only, never fetched or interpreted as public URLs.
- Ticket `issuer_hint` and customer position are inputs, never official findings.
  The demo issuer/evidence and tier eligibility are fixtures regardless of ticket facts.
  The issuer, deciding body, stage, basis and evidence reference appear in the pre-hold
  offer before authorization. No jurisdiction is inferred unsupported from a research gap.

## Endpoints

`w` and `c` below stand for the opaque workspace and case identifiers returned by intake.
Workspace creation and photo upload return 201; all other successful calls return 200.

| Method | Path | Request model | Response model | Screen/purpose |
|---|---|---|---|---|
| GET | `/health` | none | Health | Preview readiness, `mode: fake` |
| POST | `/workspaces` | CreateWorkspace | WorkspaceView | Untimed saved notes |
| GET | `/workspaces/{w}` | none | WorkspaceView | Resume/preserve intake |
| POST | `/workspaces/{w}/photos` | PhotoUpload | PhotoRef | Ticket or evidence photo (JPEG/PNG, base64, max 10 MB) |
| POST | `/workspaces/{w}/intake` | SubmitIntake | IntakeReceipt | Ticket (where issued, who issued), position, contact with phone, photo refs |
| GET | `/cases/{c}/pre-hold` | none | Offer or Stop | Resolved issuer and tiers, or plain stop |
| PUT | `/cases/{c}/tier` | ChooseTier | Offer | Explicit choice; launch offers $39 certified only |
| POST | `/cases/{c}/authorization` | StartAuthorization | Authorization | Stripe.js `pm_` id from the browser; $39 hold |
| GET | `/cases/{c}/status` | none | CaseStatus | Countdown, payment, open question or stop |
| POST | `/cases/{c}/answers` | AnswerQuestion | CaseStatus | Closed vehicle-relationship question |
| GET | `/cases/{c}/draft` | none | Draft | Exact-text review |
| PUT | `/cases/{c}/draft` | EditDraft | Draft | Text edit with expected hash |
| POST | `/cases/{c}/adoption` | AdoptText | AdoptionReceipt | Adopt exact UTF-8 text/hash |
| POST | `/cases/{c}/approval` | Approve | CaseStatus | Approve adopted current hash |
| GET | `/cases/{c}/tracking` | none | Tracking | Mailing events and report title |
| GET | `/cases/{c}/destination/disclosure` | none | DestinationDisclosure | Customer-directed destination disclosure |
| POST | `/cases/{c}/destination/address` | DestinationAddress | DestinationAddressReceipt | Customer-supplied address revision |
| POST | `/cases/{c}/destination/consent` | DestinationConsent | DestinationConsentReceipt | Explicit scoped acceptance |
| POST | `/cases/{c}/destination/decline` | none | CaseStatus | Decline: no charge, hold released |
| POST | `/internal/cases/{c}/step` | WorkerStep | CaseStatus | Authenticated fake progression |

The existing 2.x ticket/photos, statement, mailing/contact, checkout, letter preview,
confirmation, resume and status pages informed these fields. Rewire checkout to tier
choice plus authorization, and preview to exact-text editing/adoption/approval. The
mail-destination attestation/override screens have no endpoint in this contract.
Signature images, generated PDFs, webhook receivers and outcome
follow-up are deferred; exact-text adoption is not a signature-image upload.

## Flow and replay rules

1. Create a workspace, then submit intake. A replay with the same intake returns the
   same case and its current state; changing submitted intake receives 409. Workspace
   creation itself is not idempotent: keep the returned ID.
2. Read pre-hold: `kind: offer` includes a resolved issuer, $39 certified and (when
   allowed) $25 standard, with `selected_tier: null`. Inject
   `FakeServices(standard_offered=False)` for certified-only preview. Eligibility and
   deadline margins are fixtures here; production must use the canonical checks.
   `kind: stop` includes family, hard-stop code when applicable, plain `reason_key`,
   message, `no_charge: true` and `work_preserved: true`. PROCESS_INCOMPATIBLE carries
   the official requirement quote; failed verification stays NOT_YET_VERIFIED.
   A stop never creates a hold. The last example below uses an isolated stop fixture.
3. Choose a displayed tier. Authorization requires `terms_accepted: true` and returns
   an unusable `client_secret` placeholder, amount, `fake: true` and
   `status: requires_confirmation`. It does not start the clock or capture anything.
   Replays return the same intent; the chosen tier is then locked.
4. The first worker step simulates successful authorization, enters RESEARCHING and
   starts the fixed 1,800-second window. The next step asks VEHICLE_RELATIONSHIP.
   Answer with one of the returned choices (no arbitrary prose). The next worker step
   produces a fixture draft and enters AWAITING_ADOPTION. Each distinct `step_id` advances
   one available scripted step; retry the same ID to avoid advancing twice. These IDs
   are deduplication keys, not commands. There is no background runner in this preview.
5. Status reports ACTIVE, then WARNING at five minutes remaining. The window never
   resets for answers, edits or retries. At expiry before submission, status becomes
   STOPPED_NO_CHARGE and the fake hold is RELEASED. Countdown fields are null outside
   timed states; historical start/expiry timestamps remain. Stopped cases cannot authorize.
6. Editing requires `expected_sha256` for the current text. An exact edit replay is a
   no-op; changed text increments revision, saves the edit and clears adoption. SHA-256
   covers the exact UTF-8 bytes, including whitespace. Adopt by sending the same text
   and hash; approve by sending its adopted hash. Stale edits, mismatched adoption and
   approval without current adoption receive 409. Repeated matching adoption/approval
   is harmless. Approval returns FROZEN with payment still AUTHORIZED.
7. The next worker step simulates mailing acceptance, then capture, returning IN_TRANSIT
   and CAPTURED. It never calls Stripe or Lob. Tracking says acceptance, never delivery,
   and includes a visibly fake number only for certified. Standard has no certified
   tracking number. The free report title is ParkingBreaker Delivery Confirmation.
8. A stop preserves the workspace intake/notes, saved answers, edits and draft while
   this process runs. GET workspace and GET draft remain available for saved work.
   A restart is a new workspace/intake using that saved work, never a reset of the
   stopped case or its authorization window. Durable restart/research reuse is deferred.

## Errors and logs

401 means missing/invalid authentication, 404 means absent/not owned, 409 means the
current step or exact-text identity conflicts, and 422 means invalid request data.
Errors use the Error model, `{"detail":"stable_reason_key"}`; validation returns
`{"detail":"invalid_request"}` without rejected customer content. Health, docs and
OpenAPI are public and contain schemas/fixtures only.

The middleware emits only `method`, `route` (the template, never the concrete URL),
`status` and `duration_ms`. Unmatched routes use `<unmatched>` and unrecognized HTTP
methods use `OTHER`. No bodies, identifiers, headers, queries or exception details are
included. The preview entry point disables Uvicorn's raw-URL access logger and enables
only the allowlisted access logger; it does not change the root logging configuration.
Infrastructure/proxy log policy still belongs to the deployment work.

### Optional citation back

The existing intake form accepts `ticket_back_photo_ref`; final intake accepts
`citation_back_photo_ref`. Both reference the same owner-scoped photo upload store.
Omitting the optional back preserves the legacy wire representation and retry identity.
A selected back is retained across workspace reload and exact intake retries, disclosed
in the adopted enclosure list, and printed immediately after the citation front.
The actual preview and frozen packet hashes include its bytes. A missing saved back
fails explicitly rather than silently producing a front-only packet.

### Ordered optional supporting photos

`IntakeForm.evidence_photo_refs` is an optional ordered array of owner-scoped photo
references. When omitted, legacy `evidence_photo_ref` retains its existing wire
representation and supplies zero or one selection. An explicit empty array clears
the selection. If both fields are populated they must describe the same one-item
selection; conflicting values are rejected. Autosave validates every selected ref
before saving any revision. Final intake continues to use ordered `photo_refs`, and
the existing packet path includes those photos in order. Upload size and ownership
protections are unchanged.

### Verified required forms in packet preview

The existing packet preview uses saved, hash-bound official templates and the original
intake values. Required form titles appear in the draft enclosure line before adoption.
Preview includes completed form pages after the selected photo pages, and approval
freezes those exact inspected bytes. Narrative fields retain the original customer
statement exactly. Unmapped, active, unsupported or unprintable forms fail closed;
text that cannot fit is never silently clipped. No new customer endpoint or form
upload bypass is introduced. Form completion and final inventory use the existing
research and packet paths.

## Executed request/response examples

These are executed in-memory scripted fixtures, not provider or durable packet acceptance.
Customer calls use `fake-alice`; internal calls use `fake-worker`. The examples use a
fixed synthetic clock and identifiers. `test_documented_examples_match_live_routes`
compares the exact transcript and verifies route coverage. Packet preview intentionally
fails closed for this scripted service; approval hashes below are synthetic examples.

### 1. GET /health

```json
{
  "method": "GET",
  "path": "/health",
  "request": null,
  "status": 200,
  "response": {
    "status": "ok",
    "mode": "fake"
  }
}
```

### 2. POST /workspaces

```json
{
  "method": "POST",
  "path": "/workspaces",
  "request": {
    "notes": "Saved notes"
  },
  "status": 201,
  "response": {
    "workspace_id": "ws_00000000000000000000000000000001",
    "notes": "Saved notes",
    "intake": null,
    "case_id": null,
    "work_preserved": true
  }
}
```

### 3. PUT /workspaces/ws_00000000000000000000000000000001/intake-form

```json
{
  "method": "PUT",
  "path": "/workspaces/ws_00000000000000000000000000000001/intake-form",
  "request": {
    "expected_revision": 1,
    "form": {
      "values": {
        "story": "Saved partial input"
      },
      "step": 2
    }
  },
  "status": 200,
  "response": {
    "workspace_id": "ws_00000000000000000000000000000001",
    "notes": "Saved notes",
    "intake": null,
    "case_id": null,
    "draft_revision": 2,
    "intake_form": {
      "values": {
        "story": "Saved partial input"
      },
      "ticket_photo_ref": null,
      "evidence_photo_ref": null,
      "step": 2
    },
    "work_preserved": true
  }
}
```

### 4. POST /workspaces/ws_00000000000000000000000000000001/photos

```json
{
  "method": "POST",
  "path": "/workspaces/ws_00000000000000000000000000000001/photos",
  "request": {
    "content_type": "image/png",
    "data_base64": "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAFElEQVR4nGP8//8/AwwwMSAB3BwAlm4DBfIlvvkAAAAASUVORK5CYII="
  },
  "status": 201,
  "response": {
    "photo_ref": "photo_6e5edb15b6bed556ae7e6c4e156378ddb721f066f5f0e523a61106080ef8be08"
  }
}
```

### 5. POST /workspaces/ws_00000000000000000000000000000001/intake

```json
{
  "method": "POST",
  "path": "/workspaces/ws_00000000000000000000000000000001/intake",
  "request": {
    "ticket": {
      "citation_number": "DEMO-12345",
      "license_plate": "DEMO123",
      "license_plate_state": "CA",
      "issuer_hint": "Demo Municipal Parking Agency",
      "issued_city": "Demo City",
      "issued_state": "CA",
      "violation_code": "DEMO-7",
      "violation_description": "Meter expired",
      "issue_date": "2026-10-14",
      "stage": "first-level review"
    },
    "customer_position": {
      "statement": "I paid for parking and have the receipt."
    },
    "contact": {
      "name": "Alex Example",
      "phone": "415-555-0100",
      "email": "alex@example.test",
      "mailing_address": {
        "line1": "123 Example Street",
        "city": "Demo City",
        "state": "CA",
        "zip_code": "90000"
      }
    },
    "photo_refs": [
      "fake://workspace/receipt"
    ]
  },
  "status": 200,
  "response": {
    "workspace_id": "ws_00000000000000000000000000000001",
    "case_id": "case_00000000000000000000000000000001",
    "state": "INTAKE",
    "pre_hold_status": "ready"
  }
}
```

### 6. GET /workspaces/ws_00000000000000000000000000000001

```json
{
  "method": "GET",
  "path": "/workspaces/ws_00000000000000000000000000000001",
  "request": null,
  "status": 200,
  "response": {
    "workspace_id": "ws_00000000000000000000000000000001",
    "notes": "Saved notes",
    "intake": {
      "ticket": {
        "citation_number": "DEMO-12345",
        "license_plate": "DEMO123",
        "license_plate_state": "CA",
        "issuer_hint": "Demo Municipal Parking Agency",
        "issued_city": "Demo City",
        "issued_state": "CA",
        "issuer_kind": "GOVERNMENT",
        "violation_code": "DEMO-7",
        "violation_description": "Meter expired",
        "issue_date": "2026-10-14",
        "stage": "first-level review",
        "notice_date": null,
        "printed_deadline": null
      },
      "customer_position": {
        "statement": "I paid for parking and have the receipt.",
        "provision_selections": []
      },
      "contact": {
        "name": "Alex Example",
        "phone": "415-555-0100",
        "email": "alex@example.test",
        "mailing_address": {
          "line1": "123 Example Street",
          "line2": "",
          "city": "Demo City",
          "state": "CA",
          "zip_code": "90000"
        }
      },
      "citation_photo_ref": null,
      "photo_refs": [
        "fake://workspace/receipt"
      ]
    },
    "case_id": "case_00000000000000000000000000000001",
    "draft_revision": 2,
    "intake_form": {
      "values": {
        "story": "Saved partial input"
      },
      "ticket_photo_ref": null,
      "evidence_photo_ref": null,
      "step": 2
    },
    "work_preserved": true
  }
}
```

### 7. GET /auth/appeal

```json
{
  "method": "GET",
  "path": "/auth/appeal",
  "request": null,
  "status": 200,
  "response": {
    "workspace_id": "ws_00000000000000000000000000000001",
    "notes": "Saved notes",
    "intake": {
      "ticket": {
        "citation_number": "DEMO-12345",
        "license_plate": "DEMO123",
        "license_plate_state": "CA",
        "issuer_hint": "Demo Municipal Parking Agency",
        "issued_city": "Demo City",
        "issued_state": "CA",
        "issuer_kind": "GOVERNMENT",
        "violation_code": "DEMO-7",
        "violation_description": "Meter expired",
        "issue_date": "2026-10-14",
        "stage": "first-level review",
        "notice_date": null,
        "printed_deadline": null
      },
      "customer_position": {
        "statement": "I paid for parking and have the receipt.",
        "provision_selections": []
      },
      "contact": {
        "name": "Alex Example",
        "phone": "415-555-0100",
        "email": "alex@example.test",
        "mailing_address": {
          "line1": "123 Example Street",
          "line2": "",
          "city": "Demo City",
          "state": "CA",
          "zip_code": "90000"
        }
      },
      "citation_photo_ref": null,
      "photo_refs": [
        "fake://workspace/receipt"
      ]
    },
    "case_id": "case_00000000000000000000000000000001",
    "draft_revision": 2,
    "intake_form": {
      "values": {
        "story": "Saved partial input"
      },
      "ticket_photo_ref": null,
      "evidence_photo_ref": null,
      "step": 2
    },
    "work_preserved": true
  }
}
```

### 8. GET /cases/case_00000000000000000000000000000001/pre-hold

```json
{
  "method": "GET",
  "path": "/cases/case_00000000000000000000000000000001/pre-hold",
  "request": null,
  "status": 200,
  "response": {
    "kind": "offer",
    "case_id": "case_00000000000000000000000000000001",
    "issuer": {
      "issuer_id": "demo-ca",
      "issuer_name": "Demo Municipal Parking Agency",
      "deciding_body": "Demo Parking Administrative Review",
      "state": "CA",
      "stage": "first-level review",
      "service_basis": "FAKE_LOCAL_GOVERNMENT_FIXTURE",
      "delegated_tenant": null,
      "evidence_ref": "fake://pre-hold/demo-ca"
    },
    "tiers": [
      {
        "tier": "CERTIFIED",
        "amount_cents": 3900,
        "currency": "usd",
        "description": "Certified mail with tracking; no promised arrival date."
      }
    ],
    "selected_tier": null
  }
}
```

### 9. PUT /cases/case_00000000000000000000000000000001/tier

```json
{
  "method": "PUT",
  "path": "/cases/case_00000000000000000000000000000001/tier",
  "request": {
    "tier": "CERTIFIED"
  },
  "status": 200,
  "response": {
    "kind": "offer",
    "case_id": "case_00000000000000000000000000000001",
    "issuer": {
      "issuer_id": "demo-ca",
      "issuer_name": "Demo Municipal Parking Agency",
      "deciding_body": "Demo Parking Administrative Review",
      "state": "CA",
      "stage": "first-level review",
      "service_basis": "FAKE_LOCAL_GOVERNMENT_FIXTURE",
      "delegated_tenant": null,
      "evidence_ref": "fake://pre-hold/demo-ca"
    },
    "tiers": [
      {
        "tier": "CERTIFIED",
        "amount_cents": 3900,
        "currency": "usd",
        "description": "Certified mail with tracking; no promised arrival date."
      }
    ],
    "selected_tier": "CERTIFIED"
  }
}
```

### 10. POST /cases/case_00000000000000000000000000000001/authorization

```json
{
  "method": "POST",
  "path": "/cases/case_00000000000000000000000000000001/authorization",
  "request": {
    "terms_accepted": true,
    "payment_method_id": "pm_card_visa"
  },
  "status": 200,
  "response": {
    "case_id": "case_00000000000000000000000000000001",
    "payment_intent_id": "pi_fake_case_00000000000000000000000000000001",
    "client_secret": "pi_fake_case_00000000000000000000000000000001_secret_placeholder",
    "amount_cents": 3900,
    "currency": "usd",
    "status": "requires_confirmation",
    "window_started_at": null,
    "window_expires_at": null,
    "fake": true
  }
}
```

### 11. POST /internal/cases/case_00000000000000000000000000000001/step

```json
{
  "method": "POST",
  "path": "/internal/cases/case_00000000000000000000000000000001/step",
  "request": {
    "step_id": "hold-confirmed"
  },
  "status": 200,
  "response": {
    "case_id": "case_00000000000000000000000000000001",
    "workspace_id": "ws_00000000000000000000000000000001",
    "state": "RESEARCHING",
    "payment_state": "AUTHORIZED",
    "selected_tier": "CERTIFIED",
    "amount_cents": 3900,
    "window_started_at": "2026-10-14T12:00:00Z",
    "window_expires_at": "2026-10-14T12:30:00Z",
    "seconds_remaining": 1800,
    "window_status": "ACTIVE",
    "question": null,
    "stop": null,
    "research": null,
    "fake": true
  }
}
```

### 12. POST /internal/cases/case_00000000000000000000000000000001/step

```json
{
  "method": "POST",
  "path": "/internal/cases/case_00000000000000000000000000000001/step",
  "request": {
    "step_id": "research-question"
  },
  "status": 200,
  "response": {
    "case_id": "case_00000000000000000000000000000001",
    "workspace_id": "ws_00000000000000000000000000000001",
    "state": "NEEDS_CUSTOMER",
    "payment_state": "AUTHORIZED",
    "selected_tier": "CERTIFIED",
    "amount_cents": 3900,
    "window_started_at": "2026-10-14T12:00:00Z",
    "window_expires_at": "2026-10-14T12:30:00Z",
    "seconds_remaining": 1800,
    "window_status": "ACTIVE",
    "question": {
      "question_id": "VEHICLE_RELATIONSHIP",
      "prompt": "What is your relationship to the vehicle?",
      "choices": [
        "REGISTERED_OWNER",
        "DRIVER_NOT_OWNER",
        "BUSINESS_OR_FLEET_VEHICLE",
        "RENTAL_OR_LEASE_RENTER",
        "AUTHORISED_REPRESENTATIVE",
        "OTHER"
      ]
    },
    "stop": null,
    "research": null,
    "fake": true
  }
}
```

### 13. GET /cases/case_00000000000000000000000000000001/status

```json
{
  "method": "GET",
  "path": "/cases/case_00000000000000000000000000000001/status",
  "request": null,
  "status": 200,
  "response": {
    "case_id": "case_00000000000000000000000000000001",
    "workspace_id": "ws_00000000000000000000000000000001",
    "state": "NEEDS_CUSTOMER",
    "payment_state": "AUTHORIZED",
    "selected_tier": "CERTIFIED",
    "amount_cents": 3900,
    "window_started_at": "2026-10-14T12:00:00Z",
    "window_expires_at": "2026-10-14T12:30:00Z",
    "seconds_remaining": 1800,
    "window_status": "ACTIVE",
    "question": {
      "question_id": "VEHICLE_RELATIONSHIP",
      "prompt": "What is your relationship to the vehicle?",
      "choices": [
        "REGISTERED_OWNER",
        "DRIVER_NOT_OWNER",
        "BUSINESS_OR_FLEET_VEHICLE",
        "RENTAL_OR_LEASE_RENTER",
        "AUTHORISED_REPRESENTATIVE",
        "OTHER"
      ]
    },
    "stop": null,
    "research": null,
    "fake": true
  }
}
```

### 14. POST /cases/case_00000000000000000000000000000001/answers

```json
{
  "method": "POST",
  "path": "/cases/case_00000000000000000000000000000001/answers",
  "request": {
    "question_id": "VEHICLE_RELATIONSHIP",
    "answer": "REGISTERED_OWNER"
  },
  "status": 200,
  "response": {
    "case_id": "case_00000000000000000000000000000001",
    "workspace_id": "ws_00000000000000000000000000000001",
    "state": "RESEARCHING",
    "payment_state": "AUTHORIZED",
    "selected_tier": "CERTIFIED",
    "amount_cents": 3900,
    "window_started_at": "2026-10-14T12:00:00Z",
    "window_expires_at": "2026-10-14T12:30:00Z",
    "seconds_remaining": 1800,
    "window_status": "ACTIVE",
    "question": null,
    "stop": null,
    "research": null,
    "fake": true
  }
}
```

### 15. POST /internal/cases/case_00000000000000000000000000000001/step

```json
{
  "method": "POST",
  "path": "/internal/cases/case_00000000000000000000000000000001/step",
  "request": {
    "step_id": "research-draft"
  },
  "status": 200,
  "response": {
    "case_id": "case_00000000000000000000000000000001",
    "workspace_id": "ws_00000000000000000000000000000001",
    "state": "AWAITING_ADOPTION",
    "payment_state": "AUTHORIZED",
    "selected_tier": "CERTIFIED",
    "amount_cents": 3900,
    "window_started_at": "2026-10-14T12:00:00Z",
    "window_expires_at": "2026-10-14T12:30:00Z",
    "seconds_remaining": 1800,
    "window_status": "ACTIVE",
    "question": null,
    "stop": null,
    "research": null,
    "fake": true
  }
}
```

### 16. GET /cases/case_00000000000000000000000000000001/draft

```json
{
  "method": "GET",
  "path": "/cases/case_00000000000000000000000000000001/draft",
  "request": null,
  "status": 200,
  "response": {
    "case_id": "case_00000000000000000000000000000001",
    "revision": 1,
    "text": "To Demo Parking Administrative Review:\n\nI request review of citation DEMO-12345.\n\nI paid for parking and have the receipt.\n\nSincerely,\nAlex Example",
    "text_sha256": "f9bcfbd961eed3ebbd07a22e2e9e039a078eebd204c11eeac3528ef8f6d2a258",
    "adopted_sha256": null,
    "adoption_screen_text": "Here is your typed draft based on your notes. Read it, edit any details, and approve if you adopt it as your own statement."
  }
}
```

### 17. PUT /cases/case_00000000000000000000000000000001/draft

```json
{
  "method": "PUT",
  "path": "/cases/case_00000000000000000000000000000001/draft",
  "request": {
    "text": "To Demo Parking Administrative Review:\n\nI request review of citation DEMO-12345.\n\nI paid for parking and have the receipt.\n\nSincerely,\nAlex Example\nReceipt enclosed.",
    "expected_sha256": "f9bcfbd961eed3ebbd07a22e2e9e039a078eebd204c11eeac3528ef8f6d2a258"
  },
  "status": 200,
  "response": {
    "case_id": "case_00000000000000000000000000000001",
    "revision": 2,
    "text": "To Demo Parking Administrative Review:\n\nI request review of citation DEMO-12345.\n\nI paid for parking and have the receipt.\n\nSincerely,\nAlex Example\nReceipt enclosed.",
    "text_sha256": "f44724c3b230ad01e60b665b79edfc44b61f3d5af8781b4e87212246cdcba808",
    "adopted_sha256": null,
    "adoption_screen_text": "Here is your typed draft based on your notes. Read it, edit any details, and approve if you adopt it as your own statement."
  }
}
```

### 18. POST /cases/case_00000000000000000000000000000001/adoption

```json
{
  "method": "POST",
  "path": "/cases/case_00000000000000000000000000000001/adoption",
  "request": {
    "text": "To Demo Parking Administrative Review:\n\nI request review of citation DEMO-12345.\n\nI paid for parking and have the receipt.\n\nSincerely,\nAlex Example\nReceipt enclosed.",
    "text_sha256": "f44724c3b230ad01e60b665b79edfc44b61f3d5af8781b4e87212246cdcba808"
  },
  "status": 200,
  "response": {
    "case_id": "case_00000000000000000000000000000001",
    "adopted_sha256": "f44724c3b230ad01e60b665b79edfc44b61f3d5af8781b4e87212246cdcba808",
    "adopted_at": "2026-10-14T12:00:00Z"
  }
}
```

### 19. POST /cases/case_00000000000000000000000000000001/packet-preview

```json
{
  "method": "POST",
  "path": "/cases/case_00000000000000000000000000000001/packet-preview",
  "request": {
    "adopted_sha256": "f44724c3b230ad01e60b665b79edfc44b61f3d5af8781b4e87212246cdcba808"
  },
  "status": 409,
  "response": {
    "detail": "packet_preview_requires_durable_service"
  }
}
```

### 20. POST /cases/case_00000000000000000000000000000001/approval

```json
{
  "method": "POST",
  "path": "/cases/case_00000000000000000000000000000001/approval",
  "request": {
    "adopted_sha256": "f44724c3b230ad01e60b665b79edfc44b61f3d5af8781b4e87212246cdcba808",
    "packet_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "envelope_sha256": "0000000000000000000000000000000000000000000000000000000000000000"
  },
  "status": 200,
  "response": {
    "case_id": "case_00000000000000000000000000000001",
    "workspace_id": "ws_00000000000000000000000000000001",
    "state": "FROZEN",
    "payment_state": "AUTHORIZED",
    "selected_tier": "CERTIFIED",
    "amount_cents": 3900,
    "window_started_at": "2026-10-14T12:00:00Z",
    "window_expires_at": "2026-10-14T12:30:00Z",
    "seconds_remaining": 1800,
    "window_status": "ACTIVE",
    "question": null,
    "stop": null,
    "research": null,
    "fake": true
  }
}
```

### 21. POST /internal/cases/case_00000000000000000000000000000001/step

```json
{
  "method": "POST",
  "path": "/internal/cases/case_00000000000000000000000000000001/step",
  "request": {
    "step_id": "fake-mail-accepted"
  },
  "status": 200,
  "response": {
    "case_id": "case_00000000000000000000000000000001",
    "workspace_id": "ws_00000000000000000000000000000001",
    "state": "IN_TRANSIT",
    "payment_state": "CAPTURED",
    "selected_tier": "CERTIFIED",
    "amount_cents": 3900,
    "window_started_at": "2026-10-14T12:00:00Z",
    "window_expires_at": "2026-10-14T12:30:00Z",
    "seconds_remaining": null,
    "window_status": null,
    "question": null,
    "stop": null,
    "research": null,
    "fake": true
  }
}
```

### 22. GET /cases/case_00000000000000000000000000000001/tracking

```json
{
  "method": "GET",
  "path": "/cases/case_00000000000000000000000000000001/tracking",
  "request": null,
  "status": 200,
  "response": {
    "case_id": "case_00000000000000000000000000000001",
    "state": "IN_TRANSIT",
    "tier": "CERTIFIED",
    "tracking_number": "FAKE-NOT-A-USPS-NUMBER",
    "events": [
      {
        "event": "accepted",
        "occurred_at": "2026-10-14T12:00:00Z",
        "description": "Fake mailing accepted; this is not delivery confirmation."
      }
    ],
    "delivery_confirmation_title": "ParkingBreaker Delivery Confirmation",
    "fake": true
  }
}
```

### 23. POST /internal/dispatch

```json
{
  "method": "POST",
  "path": "/internal/dispatch",
  "request": null,
  "status": 200,
  "response": {
    "processed": 0,
    "failed": 0
  }
}
```

### 24. POST /webhooks/stripe

```json
{
  "method": "POST",
  "path": "/webhooks/stripe",
  "request": {},
  "status": 400,
  "response": {
    "detail": "invalid_signature"
  }
}
```

### 25. POST /webhooks/lob

```json
{
  "method": "POST",
  "path": "/webhooks/lob",
  "request": {},
  "status": 400,
  "response": {
    "detail": "invalid_signature"
  }
}
```

### 26. GET /cases/case_00000000000000000000000000000001/pre-hold

```json
{
  "method": "GET",
  "path": "/cases/case_00000000000000000000000000000001/pre-hold",
  "request": null,
  "status": 200,
  "response": {
    "kind": "stop",
    "case_id": "case_00000000000000000000000000000001",
    "family": "NOT_YET_VERIFIED",
    "hard_stop": "S2",
    "reason_key": "contest_destination_unverified",
    "message": "We could not verify where this contest must be mailed. You were not charged.",
    "no_charge": true,
    "work_preserved": true,
    "official_quote": null
  }
}
```

### 27. GET /cases/case_00000000000000000000000000000001/destination/disclosure

```json
{
  "method": "GET",
  "path": "/cases/case_00000000000000000000000000000001/destination/disclosure",
  "request": null,
  "status": 200,
  "response": {
  "version": "v1",
  "sha256": "ea45973d5e3be79d914e043bec37817365c32de6b2cd17237d44e596fea9d186",
  "text": "We could not confirm the official appeal mailing address from the agency's official sources.\n\nYou can enter the mailing address yourself, and we will mail your approved appeal to that exact address. Make sure you enter the agency's contest or appeal address, not the payment address.\n\nBecause we could not verify this address, we cannot confirm that it is the agency's current appeal office, that USPS can deliver to it, or that the agency will accept mail there.\n\nHow refunds work for an address you supply: if your letter is undeliverable, returned, or not accepted solely because the address you entered is wrong, our normal refund for an address error does not apply. Every other mistake of ours remains our responsibility, including mailing to any address other than the exact one you approved, leaving out required contents, charging you twice, or any failure of our service. Nothing here limits your rights under applicable law.\n\nPrefer not to take that risk? You can decline right now. Your card will not be charged, the hold will be released, and your work will stay saved."
}
}
```

### 28. POST /cases/case_00000000000000000000000000000001/destination/address

```json
{
  "method": "POST",
  "path": "/cases/case_00000000000000000000000000000001/destination/address",
  "request": {
    "recipient": "Parking Review Office",
    "line1": "1 Example Plaza",
    "city": "Demo City",
    "state": "CA",
    "zip_code": "90000"
  },
  "status": 409,
  "response": {
    "detail": "destination_choice_requires_durable_service"
  }
}
```

### 29. POST /cases/case_00000000000000000000000000000001/destination/consent

```json
{
  "method": "POST",
  "path": "/cases/case_00000000000000000000000000000001/destination/consent",
  "request": {
    "address_revision": 1,
    "address_fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "disclosure_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "accepted": true
  },
  "status": 409,
  "response": {
    "detail": "destination_choice_requires_durable_service"
  }
}
```

### 30. POST /cases/case_00000000000000000000000000000001/destination/decline

```json
{
  "method": "POST",
  "path": "/cases/case_00000000000000000000000000000001/destination/decline",
  "request": null,
  "status": 409,
  "response": {
    "detail": "destination_choice_requires_durable_service"
  }
}
```
