# Controlled beta operating packet

Prepared November 15, 2026 from application commit
`e4e4a28a912c84c34e66044f9e47ae7aba3d6b4b`; evidence updated November 16.
See [current readiness](../BETA-READINESS.md) for source-specific results.
Status: **NO-GO for live intake**. This is a preparation and acceptance packet,
not permission to provision, deploy, use paid providers, send notifications or mail.

## Checking the government domain directory on a qualification target

Read the file mounted at `PB_DOTGOV_SNAPSHOT` inside the running customer backend. Record its source date, record count and SHA-256. Compare its bytes with the full CISA directory at the selected public revision. For each rehearsal ticket, record the issuer candidates before starting billable research. On November 26 a recent, valid one-row fixture prevented both test cities from matching. The full registry corrected candidate selection. Verify the running worker as well as the API. Both standing containers still read a one-row file after the isolated rehearsal was corrected. The private VM Compose file mounts the host data directory at `/data` in both roles. A date-stamped registry stored there must use its container-visible path in `PB_DOTGOV_SNAPSHOT`. Compare each role’s file hash with the pinned source before qualification. [CISA source](https://get.gov/about/data/) · [Deployment receipt](../verification/r01-candidate-2026-11-26.json).

## Paid launch direction (November 17)

Paid launch uses the existing authorization, exact-packet approval, mailing and capture
controls. The November 23 ordinary-mail pilot has its own enrollment and qualification
described below. New intake requires ticket
images and an OCR attempt before fact confirmation; correction does not replace reading.
The intended correct-office/on-time commitment requires verified official procedure and
provider qualification before customer admission. See [Customer Choice Rules](../CUSTOMER-CHOICE-RULES.md).

## Customer boundary

Ask for a readable ticket and any required back page. Accept JPEG/PNG, at most
10,000,000 bytes per image. For HEIC/PDF or unreadable images, explain how to export
or retake a readable image. The customer enters/corrects missing facts and confirms
the exact facts and packet. Do not guess facts, legal conclusions or deadlines.
If an essential fact or a safe fulfillment condition remains unknown, stop before
charge or dispatch. No issuer-by-issuer lookup project or city allowlist is a launch
prerequisite. Existing per-request source checks and safe refusals remain in force.

## Ordinary First-Class feedback pilot

The proposed pilot admits up to five selected cases for one issuing authority and filing
stage, with enrollment closing 14 days after opening or when the fifth slot is selected.
Participants pay zero, approve the exact packet, and can report a return, agency response
or decision through their private case. Existing accepted cases retain their terms and
support after enrollment closes.

Before activation, obtain dated official evidence for ordinary mailed contests, the exact
receiving office, filing deadline, required enclosures, signatures and any original-ticket
requirement. Check that the service can prepare and mail the customer's approved copies.
Use both sides of an applicable ticket to identify the procedure. Keep unresolved conflicts
with the agency for confirmation. Qualify the actual print, return handling, authenticated
postal events, feedback delivery and restart behavior on the intended durable deployment.
The paid Certified Mail and Stripe qualification remains a separate release decision.

On an authorized target, run the route `evidence` and `enroll` commands below with
`--mail-class first_class`. The official instructions must direct the customer to file
the contest by mail in the same issuer and filing stage as the verified address.
The `first_class_basis` field in `evidence` and `show` explains how the route qualified.

- `explicit_ordinary_mail` means the agency names ordinary, regular or First-Class Mail.
  An unconditional choice between ordinary and Certified Mail also qualifies.
- `inferred_from_unqualified_contest_mail` means the agency authorizes filing by mail
  without specifying a special service. The operator can inspect the preserved official
  passage supporting that inference.

At enrollment, the checker examines applicable instructions across the saved official
sources. A ban on ordinary mail, required tracking or proof of mailing, mandatory
courier or online filing, or a requirement for prior permission blocks enrollment.
A payment address, a mailing deadline or a notice sent by the agency cannot establish
a contest route. An unrecognized stage heading leaves the route unresolved.

Once the monitor is running, use the private command below to open the reviewed route.

```sh
python -m parkingbreaker.beta_campaign_cli --database /data/parkingbreaker.sqlite open-first-class \
  --campaign-id "$campaign_id" --issuer "$issuer_id" --stage "$stage" \
  --identity-policy-ref "$issuer_review" --configuration-ref "$pilot_approval" \
  --evidence-sha256 "$official_digest"
```

In the controlled free-beta phase, an eligible intake submission can select a slot.
Check `beta_campaign_cli --database /data/parkingbreaker.sqlite status` for the opening,
closing and selection count. Each customer must accept the First-Class disclosure.
The free offer never authorizes a credit card. Check release mode, the current route
and a genuine official source before opening any cohort on a live database.

Count every selected case when evaluating the pilot. Track returned or redirected mail,
agency acknowledgment or a request for more information, and the eventual disposition
separately. A decision upholding a ticket can confirm that the correct office processed
the filing. An unanswered reminder leaves the outcome unknown. When mailing evidence or
a customer report is absent, keep that absence visible in the pilot review. The free offer
discloses up to two feedback emails, due at 14 and 30 days from a dated postal scan
or authenticated printer handoff. Each includes an opt-out. Participants can also report
directly through their case.
Use the private report for counts of selected cases, acceptances, dated handoffs and scans,
and the customer answers. Each question includes its own count of unreported answers.

For a first-stage pilot, tell drivers before approval what they would be deciding if the
agency upholds the ticket. For example, SFMTA generally requires a deposit equal to the fine
to request a second-level hearing. It lists waiver categories and refunds the deposit if
the citation is dismissed. Link to the current waiver instructions and use the driver's
denial letter to identify the applicable deadline. The driver decides whether to continue.
[SFMTA contest and hearing instructions](https://www.sfmta.com/getting-around/drive-park/citations/contest-citation)

```sh
python -m parkingbreaker.beta_campaign_cli --database /data/parkingbreaker.sqlite report
```

## Monitoring a confirmed mailing office

Enroll an issuer, filing stage and mail class when saved case research contains an official
passage that supports both the destination and permission to file by mail. The ticket back,
Reddit accounts and department contacts can identify which office or notice to investigate.
An enrollment requires a fresh fetch of the qualified official source.

Run the following from the owning worker environment. Supply the existing case ID and use
the fingerprint and evidence digest returned by the first command. Choose an explicit
UTC expiry and a unique command ID. The first enrollment expects revision zero.

```sh
python -m parkingbreaker.route_monitor --database /data/parkingbreaker.sqlite evidence \
  --case-id "$case_id"
python -m parkingbreaker.route_monitor --database /data/parkingbreaker.sqlite enroll \
  --case-id "$case_id" --address-fingerprint "$fingerprint" --evidence-sha256 "$digest" \
  --valid-until "$expires" --expected-revision 0 --command-id "$command_id"
```

On an approved private VM deployment, install the route monitor service and timer from
`deploy/systemd/`, then enable `parkingbreaker-route-monitor.timer`. Inspect the service
journal for each route's result. The timer checks every 15 minutes. Changed normalized text
on the single enrolled URL, a missing official passage, or an HTTP 404/410 fences the old
revision. An unavailable page cannot refresh the last good check. After one hour without a
successful check, new mail stops even if the timer itself has failed. The enrollment also has a hard expiry.
If an office moves while its published page stays the same, the watcher has no signal
of that move. A department confirmation can trigger an immediate scoped revocation.

For a move confirmed directly by the department, run `show` for the issuer and stage,
then `revoke` with that scope, its current `--expected-revision` and a unique
`--command-id`. A replacement requires fresh official evidence and a new revision.
An approval tied to the old route cannot mail a changed envelope. Reconcile any submission
already underway by its original provider reference. After a database restore, preserve
quarantine while reconciling route revisions with recorded moves before resuming dispatch.

## Configuration and provider access

Use the reviewed `.env.example` as a **nonsecret template**, not the archived `.env`.
The example selects `fake`, `paid`, DeepSeek V4.1 Flash intake
(`deepseek-flash`) and the existing DeepSeek research/drafting roles. The optional
Gemini packet-check preset is retained; the current customer-authored service does
not construct a packet-check model. `PB_LLM_PRESET` must be absent:
even a blank value overrides role selection. Unused support/digest preset fallback
code still names MiniMax; it is not selected by these four role settings.
Changing the example does not change the running service or grant beta entitlements.
The November 18 [single-call OCR receipt](../verification/p04-deepseek-single-2026-11-18.json)
records the approved clear-ticket run and its token cost estimate.

Before any real-provider startup, record secure credential references, account/mode,
approved privacy/retention terms, available funding and the exact allowed calls.
Do not copy the archived credential file wholesale: its credentials and settings are not current qualification evidence.
The canonical checkout has no credential file. Never put secrets in receipts or Git.

Owner requested the ticket-reader switch on November 15. DeepSeek's current
[vision guide](https://api-docs.deepseek.com/guides/vision/) supports inline images
with `deepseek-flash`; its [model table](https://api-docs.deepseek.com/quick_start/pricing/)
identifies V4.1 Flash. The adapter structurally validates JPEG/PNG, preserves original
image detail, rejects Pro image requests, bounds body size and uses the documented
`max_tokens` output cap. It reserves 8192 input tokens per image against the documented
1024-image-token maximum, plus the existing text/framing allocation. This is an
offline conservative allowance, not a live billing or reading-quality qualification.
Extraction and fresh comparison still require the customer's exact facts and approval.

Observed November 15: Gemini model-list authentication succeeds. The inherited
DeepSeek process credential returned 401; the owner-supplied key later passed models
and balance GETs (200), with Flash available and funding present. Private Ops retains
the credential reference and account balance; no key was copied into the checkout.
Brave has no configured runtime credential. The native DeepSeek GUI is not listening
on port 3080.
No paid worker was started. The supervisor implemented the bounded adapter change
locally after reporting that the default native DeepSeek worker was unavailable.
The six original owner-provided fictional images are hash-verified and held separately
from the answer key. Two specifically approved trial envelopes completed 15 unique
calls, reserving $0.51 in total, with a conservative local usage estimate of $0.15
(not a provider invoice). No automatic retries occurred. A/B/E passed on the tested
source; strict damaged/ambiguous-case qualification remains incomplete. The latest
prompt changes have offline checks only. See the [corrected live assessment](../verification/deepseek-corrected-live-2026-11-16.json)
for the diagnostic-versus-production distinction. No additional paid run is approved.

The 23-row research campaign remains blocked by unsigned keys and account access.
All 109 saved quotations match; B09/B13/B16 are mechanically ready for review, S05
needs scope-only review, and 19 other rows retain explicit uncertainties. A signature
cannot turn an unknown fact into a known one. Keep these as qualification fixtures,
not a new issuer-specific production database. Do not stamp the rows automatically.
The previously proposed $62 research / $8 image budgets are **unapproved**; refresh
account rates and run bounds before presenting any exact spending request.

### Three-price checkout report

ParkingBreaker assigns one certified-mail price per recognized visitor. The three
one-time USD prices are $19.99 (`P1999`), $24.99 (`P2499`) and $29.99 (`P2999`).
The server authorizes the frozen offer amount on a manual-capture PaymentIntent.
For a matching, recorded assignment, a new authorization carries `price_arm` and
`price_experiment_id=t56-price-test-2026-10-30`. A retry uses the original metadata.

In the correct Stripe account and mode, search `is:payment metadata:price_arm=P1999`.
Substitute `P2499` or `P2999` to see the other prices. Add `is:captured` for collected
payments or `is:refunded` for payments with a refund. [Stripe Dashboard search](https://docs.stripe.com/dashboard/search)

Run the following from the checkout root with the path to the verified transaction
database for the environment being measured.

```bash
backend/.venv/bin/python -m parkingbreaker.price_funnel /path/to/parkingbreaker.sqlite
```

The read-only report counts browser assignments, matching certified offers recorded
by the server, appeals with a tagged authorization attempt, confirmed holds,
captures, refunded appeals and gross captured cents before refunds and fees. A
repeat attempt on an appeal counts once. Assignment and offer counts can predate
the Stripe labels. A recorded offer provides no evidence that the customer saw it.
Use the production database for customer results and the sandbox database for drills.

### Paid cohort payment and mailing review

Use the launch window's authorized start time and the transaction database for
the environment being reviewed. Run this read-only report from the checkout root.

```bash
backend/.venv/bin/python -m parkingbreaker.paid_beta_report /path/to/parkingbreaker.sqlite \
  --since 2026-11-20T17:00:00-08:00
```

The cohort includes paid cases whose confirmed authorization began on or after
the provided time. Its totals reflect one current SQLite snapshot. It includes
recorded gross captures and exact confirmed refunds, cases with unfinished payment
or mailing work, and local authorization deadlines. It also reports unresolved
pre-case authorizations across all dates, so older uncertain effects stay visible.
Use the prior window's actual start time when reviewing a different cohort.

Check unknown capture outcomes, unknown refund amounts and expired holds against
the original provider records before closing the review. Fees, payouts, disputes,
attribution and other operating costs require separate contribution evidence.

The report also includes an all-date original-capture review independent of
`--since`. Its fallback share uses all unique successful original charges, including
sales later refunded. Each fallback record carries subsequent USPS evidence, verified
city receipt, confirmed non-mailing, conflicting evidence, refund progress or an
unresolved finding. The report may include an internal operations reference. Keep it
private. It contains no raw customer, provider or payment evidence.

At the twentieth recorded successful capture with Stripe-confirmed live mode,
the report marks the first-20 review due. In the production database, membership
is fixed by recorded capture order, even when evidence, disputes or refunds arrive
later. That snapshot covers the denominator, fallback numerator and share, unknown
bases, and each fallback outcome. Test-mode and mode-unknown records appear in
their own totals. Review any historical mode-unknown records separately before
calling the sample the first 20 actual customer sales.

The owner or assigned operations reviewer records the first review in the paid
activation record, reconciles any conflicts, confirms full refunds against the
original charge and assigns unresolved items. Rerun this report as new evidence arrives
until each item is resolved or recorded as unresolved with an owner and a next check
date. Record the decision about the cap or paid intake through the existing R07
process. The report has no checkout controls.

### Paid charge timing and exact-letter follow-up

The checkout, rules, status and legal pages use the shared
[`PAID_CAPTURE_TERMS` and `NONMAILING_REFUND`](../../web/lib/legal.ts) copy.
The customer sees the usual USPS scan trigger before authorizing, alongside the
hold-expiry condition and full original-charge refund promise. The fallback
requires a fresh, exact original-letter lookup when the hold is close to expiry,
PDF rendered, scheduled processing time passed, no qualifying scan and no
failure, deletion or cancellation. Lob describes this date as when processing
can start and the earlier cancellation window ends. A rendered PDF, even after
that date, does not prove printing or postal handoff. The FTC guidance covers
substantiation of express and implied claims and placing material qualifications
beside the claim. Source review completed November 25, 2026. Human counsel review
is pending. [Lob scheduling documentation](https://help.lob.com/print-and-mail/building-a-mail-strategy/choosing-a-delivery-strategy) · [Lob letter events](https://docs.lob.com/#tag/Letters) · [FTC advertising guidance](https://www.ftc.gov/business-guidance/resources/advertising-faqs-guide-small-business)

Retain the original response body, letter ID, observed time, send date and
SHA-256 in the restricted transaction database as part of the same write as
the fallback decision. The active worker removes raw bodies after 90 days
unless a recorded preservation hold applies. The restricted decision fields
and digest remain for financial review. This is provider evidence that may
include personal information, so keep it out of diagnostics, exports and
customer-facing responses. Existing WAL files and backups follow the private
backup lifecycle. Restoration and backup-copy retention still require target
verification under R05. Use the protected evidence and direct provider access
for reconciliation. Older fallback charges may have an explicit capture
reason and no raw response on file. The 90-day active period is shorter than the
published ticket-data and financial-record maximums. The mixed-content body is
removed separately from the financial decision record.

Authenticated, exact-letter Lob cancellation can support an automatic full
refund. A generic failure notification triggers an exact-letter lookup.
The worker can recheck unresolved fallback cases once per day, up to 30 attempts,
so a missed webhook can still be recovered. Signed webhooks and verified city
records can update later. Nonreceipt, no scan, a missing lookup and a returned item are insufficient
to establish non-mailing. A verified USPS scan, city receipt or credible printer
handoff conflicting with a non-mailing record stops a new automatic refund
mutation and calls for operations review. A settled full refund is counted once.
The existing worker checks the original charge and its complete refund list,
and can refund the amount remaining after a confirmed partial refund. A Stripe
refusal or unresolved refund remains in the report for reconciliation. A historical
capture without enough original-charge evidence requires staff reconciliation.
Conflicting evidence recorded after a full refund remains visible to staff.

Cities do not provide a uniform machine-readable receipt for this service.
Authorized staff may separately verify an official agency response and tie it to
the exact original letter using the agency case and correspondence details. For
non-mailing, require an affirmative authoritative record that establishes the
original was never mailed. A search that found no matching letter at the city or
a customer's report does not establish that fact. Keep the source record under
existing private evidence controls and record its digest as the database owner
through the host-local intake. Use one official evidence record per finding.

```bash
backend/.venv/bin/python -m parkingbreaker.verify_city_mail /path/to/private.sqlite \
  /path/to/private-official-record --case INTERNAL_TOKEN --letter ORIGINAL_LETTER_ID \
  --finding city_receipt --attest-verified
```

Select `never_mailed` only for affirmative non-mailing evidence. The command
records the OS verifier, exact match and evidence digest locally. It does not
make a provider call. The normal worker reconciles and dispatches any resulting
refund intent.

On the intended qualification candidate, exercise the scan-triggered path and the
hold-expiry fallback with Stripe test mode and controlled Lob evidence, including
late scans, unknown evidence and a never-mailed refund after restart. A scan-only
walkthrough covers one route. Live-mode self-charges are excluded from testing.
Record the results against the existing paid release gates. [Stripe testing guidance](https://docs.stripe.com/testing#how-to-use-test-cards)

Three terminal declines or confirmed cancellations on an appeal within 24 hours
pause new card attempts for that appeal. Existing intent replay and reconciliation
continue. The window starts from each intent's original reservation time.

## Target security acceptance

Historical November 15 GCP inventory (not refreshed for this publication):
project `neuraldraft-parkingbreaker`, region `us-west1`;
no Compute VM or persistent disk. The existing Cloud Run service is an older
frontend/backend sidecar deployment in fake/paid mode, not this qualified target.
Its restricted `run.app` ingress returns 404 externally; this alone does not assess
the public load-balancer route. Do not infer production health from Cloud Run Ready.

Prepare the following exact values before a provisioning/deployment approval:

| Required value or proof | Acceptance |
| --- | --- |
| Backend VM, zone, service identity and cost cap | Named, approved; no public IP; least required permissions |
| Regional persistent disk, device and mount | One fenced VM owns it; existing ext4 RW mount; explicit backup reserve |
| Customer origin, VPC range/tag, private DNS and TLS | Only customer gateway TCP 8443 reachable from frontend; trusted TLS |
| Backend/worker/monitor and frontend images | Immutable digests built from the reviewed commit; exact build-time API origin |
| Environment file | Root-owned regular 0600, outside Git; explicit mode and phase; separate role secrets |
| Ingress | Public `/internal`, `/dev-worker` and prefixed variants refused; worker never published |
| Old release and database schema | Recorded rollback image/topology; compatibility proven on a copy |
| Recovery destination | Off-host encrypted copy, retention/access owner and restore evidence |

`deploy/validate.sh --dry-run` checks local syntax only. Target validation requires
the actual environment path, mounted disk and fresh disk description. The private
VM overlay is selected by lifecycle and scheduled backup. Set
`PB_DEPLOY_TOPOLOGY=cloudrun-private-vm` in the provisioning environment and in
the backup timer's root-owned `ops.env`. Supply the exact Direct VPC frontend subnet
as `PB_FRONTEND_SUBNET_CIDR` and the private VM address as `PB_PRIVATE_VM_IP`.
The provisioning dry run prints the rules used by apply. Verify the cloud firewall,
effective Docker-published port, trusted TLS and real frontend/service identity on
the isolated target. A host firewall rule alone cannot establish those facts.

With the private topology selected, the backup timer installer schedules replication
at minutes 00 and 30. The monitor checks the source snapshot time recorded after a
verified off-host copy and raises a critical alert once that snapshot is over 60
minutes old. Record the installed cadence and measure the actual restore point in
the recovery drill. The single-VM timer runs hourly.

Submit both builds from a clean export of the same signed commit. Pass its full
SHA as `_PB_BUILD_COMMIT` to both Cloud Build recipes and set `_IMAGE_TAG`
to a candidate-specific value (for example, the SHA followed by `-test`).
Both final images carry the OCI revision label. Verify it against the signed
commit on each returned digest before promotion. Build the qualification frontend
with `_PB_API_ORIGIN` set to the trusted private HTTPS origin on port 8443.
Set `_PB_PROVIDER_MODE=test` and `_NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY` to the
matching Stripe test publishable key for the sandbox rehearsal. The Docker build
passes both to Next.js, which checks the key prefix against the build mode.
Record the returned registry digests and build source. Each immutable VM release
directory needs `deploy/private-images.json` in this Compose override format.
Replace the placeholders with full registry digest references and the full Git commit.

```json
{
  "x-pb-build-commit": "<40 lowercase hex characters>",
  "services": {
    "api": {"image": "<backend repository>@sha256:<64 hex>"},
    "worker": {"image": "<same backend repository>@sha256:<same digest>"},
    "monitor": {"image": "<same backend repository>@sha256:<same digest>"},
    "caddy": {"image": "<reviewed gateway repository>@sha256:<64 hex>"},
    "scheduler": {"image": "<reviewed scheduler repository>@sha256:<64 hex>"}
  }
}
```

Set `PB_BUILD_COMMIT` and `PB_PROVIDER_MODE` in the lifecycle environment and
the backup job's protected configuration for that release. Validate the lock with
`python3 deploy/private-images.py "$PB_RELEASE/deploy/private-images.json" "$PB_BUILD_COMMIT"`.
Deploy and rollback pull the locked images before stopping the old owner. Both
the backend image revision label and the private and public health responses must
match the selected release. The ingress probe needs an approved path to the
qualification hostname from the VM. A sign-in response or the preview's healthy
response fails the check. Preserve the outgoing lock with its release directory.

For an infrastructure-only drill in fake mode with separate customer and worker
processes, set `PB_FAKE_SPLIT_FOR_MEASUREMENT=1` on the isolated fixture.
Their payment and mailing simulators keep separate in-memory records. Use the
approved provider test accounts for cross-process payment and mailing recovery.

Use the existing lifecycle after exact approval, with its validation, exclusive
writer, snapshot, migration/compatibility, health and readiness gates. Do not bypass
it with an ad hoc `compose up`. No cloud creation command has been executed here.

## Intake stop and incident handling

<!-- TODO(P06/R03): Generate the missing-callback example from the drill. Show the worker
     comparing provider delivery, local receipt and saved cursors against both deadlines.
     Resolve a match on the original attempt automatically. For a conflict, show the
     evidence, decision needed, reply deadline and primary-to-backup handoff. Rehearse
     the handoff with the worker or VM down and verify acknowledgment before the cutoff. -->
These are different controls; record which one an incident needs.

1. **Stop new paid authorizations:** the existing private service-phase CLI stores
   a revision-checked override. It does not cancel existing holds, replay uncertain
   sends, stop admitted free cases or close every intake route.
2. **Stop new free-beta admission.** Legacy batches require explicit selection. For the
   ordinary pilot, the durable intake pause stops automatic selection and new acceptance.
   A changed office also suspends the affected route. Preserve existing cases for recovery
   and support.
3. **Stop all new intake.** The private gateway supports `PB_NEW_INTAKE=paused`.
   On an approved target, apply the gateway setting and verify the effective rule
   on every reachable customer ingress. Status, sign-in recovery and provider
   callbacks continue on their existing routes.
4. **Suspected duplicate dispatch or compromised target:** require the authorized
   operator to stop scheduler and worker on the one owning host, preserve the database,
   queue, original provider IDs and logs, and reconcile uncertain outcomes before
   restart. Do not disable required reconciliation blindly or submit a fresh letter.

Approved operator command pattern, after target identity, backup and revision check:

```sh
python -m parkingbreaker.service_phase_cli --database "$PB_DATABASE" status
python -m parkingbreaker.service_phase_cli --database "$PB_DATABASE" set \
  --phase controlled_free_beta --expected-revision "$PB_EXPECTED_REVISION" \
  --command-id "$PB_INCIDENT_ID" --actor "$PB_OPERATOR" \
  --reason 'Pause new paid authorizations'
```

Record before/after revision, command ID, operator, reason and approval reference.
Verify a fresh authorization is refused with no payment call, another process sees
the override, a stale revision fails, and an uncertain existing hold is reconciled
against its original intent. `test_runtime_service_phase.py` covers these locally.
No target database was opened or altered during this preparation.

The gateway's intake pause refuses POST on `/api/auth/appeals`, `/api/workspaces`
and `/api/workspaces/{id}/intake` after the frontend rewrite. The first route
creates a new appeal session. A paused gateway returns 503 for all three routes.
The default is `open` for compatibility. Any other explicit value pauses intake.
For an approved emergency change, retain the active release and locked images,
persist the setting in the protected configuration used on restart, record its
previous value, and recreate only the Caddy service with the same
Compose inputs and project name as the lifecycle, using `up -d --no-deps caddy`.
Verify existing customer access after the gateway is ready. Record the reverse
operation and verify reopening before declaring the incident resolved.
Requests already forwarded can complete. Pair the ingress pause with the
transactional paid-authorization stop and a freeze on new free selections.
Check a workspace created before the stop that has yet to submit intake, a fresh
signup and a previously selected offer separately. An already entitled or funded
case keeps its obligations. Exercise owner status/tracking GET, signed Stripe and
Lob callback POST and private reconciliation against each original effect. Record
the method, normalized route, HTTP result, durable before/after state and provider
call counts. Confirm that every other reachable ingress enforces the same stop.
Restore the prior rule and service-phase revision through the operator workflow.

For the independent alert, probe the isolated frontend's proxied `/api/health`
from outside the VM once per minute. Record last success, fault onset, first
failed probe, operator delivery, acknowledgment and clearance in UTC. Allow five
minutes from host loss to alert delivery. Separately exercise the three-minute
stale scheduler, ten-minute unsuccessful sweep, overdue queued work, wrong owner
and low disk signals. Use bounded synthetic fixtures for queue and disk pressure.
Record target observations and the primary/backup human response separately.

## Recovery and rollback

The executed local recovery rehearsal uses only synthetic storage and fake providers.
It proves backup/restore record and artifact equality, quarantine, original-intent
reconciliation, and no second fake mailing after three dispatch attempts. It is not
an off-site or production restore qualification.

For the actual approved target drill: back up using `parkingbreaker.recovery_cli
backup`, verify the manifest and off-host copy, restore into a new isolated directory,
keep `RESTORE_QUARANTINE.json`, prove the original host is fenced, and reconcile the
original-account Stripe/Lob IDs before unquarantine. From a fenced, completed restore,
export the original lookup anchors to a new private path outside the restore:

```bash
python -m parkingbreaker.recovery_cli reconcile \
  --data /private/restored --output /private/evidence/provider-inventory.json
```

The command exits 3 with `NEEDS_EXTERNAL`. Use the restricted report to match the
Stripe PaymentIntent and Lob submission token or letter ID against the approved
provider accounts. Compare the saved authorization amount and expiry with the
original Stripe objects. Track the customer review deadline separately. Compare the
exact PDF, envelope and approval fingerprints to the joined payment and mail
rehearsal. The report only covers records in the backup.
Account identity, post-backup effects and attempts with missing anchors require
independent evidence. Keep the report private because it contains original references.
Never restore over the current database or remove quarantine to make startup pass.
Rollback must retain authority data and pass compatibility on a copy; otherwise roll
forward. A `PROCESSING_BLOCKED` lifecycle marker is not a global intake kill switch.

For the private topology set `PB_REPLICA_DIRECTORY` to the mountpoint of the
independent network filesystem. A missed mount raises a backup-job failure while
preserving the local bundle. The job also checks the filesystem inside the replica
container before it copies. During the drill verify remote host identity, encrypted
transport, scoped access and receiver-side manifest hashes. Use a bundle produced
by the hourly timer, then fence the original host and restore without reading
its disk. Record the last recovered committed marker before the outage and the
first time it is safe to resume. Preserve quarantine while any provider effect
is unresolved. The [R02 preflight receipt](../verification/r02-target-preflight-2026-11-17.json)
identifies the synthetic target, cost assumptions and missing external evidence.

## Printing and notifications

Local packet tests verify US Letter geometry, page limits, approved PDF byte binding,
Lob idempotency and ambiguous-result handling. They do not prove provider output.
Next approved TEST-account qualification must bind the original PDF digest to the
provider proof, inspect all pages for cropping, readability and address placement,
and verify signed webhook account/mode and duplicate-event handling. No live letter
or customer submission is authorized. A physical proof remains separate evidence.

The existing monitor sends through Resend only when sender, key and recipient are
configured; without them it logs rejected notifications and retries. Local tests cover
outbox/monitor behavior. Before release, approve one synthetic message to a named
operator inbox, verify actual receipt and acknowledgement, and test rejection/retry.
An in-VM monitor cannot detect total VM loss: prepare a separately hosted health check
and named responder. Do not claim notifications work based on a healthy HTTP response.

## Human support and physical devices

The owner must name a primary responder, backup, coverage hours/time zone, weekend
handoff, acknowledgement target and escalation channel. Record support intake,
wrong/missing facts, unsupported input, deadline urgency, provider uncertainty and
intake-stop drills. Do not assign an AI assistant as the human coverage owner.
Customer copy and workflow review remains outstanding; this packet offers no legal
conclusion about individual tickets.

For **each actual iPhone/Safari and Android/Chrome**, record device/OS/browser,
application commit/fingerprint, synthetic fixture hash, timestamp and observer:

- Camera, library and Files selection; readable front/back, orientation, all edges.
- Actual JPEG/PNG acceptance and HEIC/PDF recovery; corrupt, empty and oversized refusal.
- Cancel, background/return and connectivity interruption without duplicate work.
- Customer correction of missing/unreadable facts; no guessed issuer/stage/deadline.
- Reload preserves exact image/fact binding; unresolved essential facts block charge/dispatch.
- Usable focus, scrolling, errors and controls; retain observations for every failure.

Use synthetic tickets through an approved secure local/staging connection with fake
providers. An attached A56, Android 13 / Chrome 157.0.8103.78, passed actual native Files
JPEG/PNG upload, picker cancel, required-fact blocking, correction/confirmation/reload
and changed-fact invalidation on `e4e4a28`. Invalid-file and injected network-failure
recovery also passed in that real browser. No charge or dispatch request occurred.
Background/return retained the partial facts with no duplicate upload. A subsequent
physical camera capture, native gallery selection, JPEG upload and reload passed;
stored bytes matched the metadata-stripping filter and decoded pixels were preserved.
The monitor photo stays private and was not sent to DeepSeek. Android landscape
rotation and iPhone qualification remain open. USB localhost used an origin adapter: it does not
qualify production TLS/DNS/origin policy. Fake readings do not qualify DeepSeek OCR.
A fresh smoke test on the subsequent source fingerprint
`2270ee89f5c9f41502841c27800103e4e599c5d2be5b1471144c1adbaebe0a5c` passed
the same native Files, refusal, confirmation, reload, edit and background/recovery
boundaries with no page errors or charge/dispatch requests. These use fake providers;
the separately approved real image trials above do not qualify the complete customer
flow or the newest prompt changes.

## Final release decision

For the funded cap, use available Lob balance and the billed cost of the exact
packet. Reserve one proof piece and the planned remails in Lob separately from
cash for full customer refunds. The November 27 planning check puts a five-page
black-and-white Certified piece at $8.42 under Lob's published Developer
rate. With one proof and one remail per customer, two customers require an
estimated $42.10 in Lob funds. Up to $62.32 in separate cash covers two
$29.99 refunds and their estimated original domestic card fees. Five customers
would require an estimated $92.62 in Lob funds and up to $155.80 in separate
cash under the same assumptions. The observed sandbox proof had three
approved pages and five rendered pages. Use the invoice to determine which
pages were billed. [Planning check](../verification/launch-qualification-2026-11-27.json)
· [Lob rate schedule](https://help.lob.com/print-and-mail/ready-to-get-started/pricing-details)
· [Stripe rates](https://stripe.com/pricing?locale=en-US)

Do not release until provider, target, printing, notification, human and physical-device
receipts identify the exact candidate and pass. The paid-beta activation packet must
specify the start/end instants, price and customer disclosures, funded/reserve ceiling,
enforced paid-cohort cap, intake-stop owner, stop conditions and explicit activation
authority under [R07](../ROADMAP.md). Do not select a free entitlement batch for this
launch; preserve the recorded obligations of historical no-charge cases. A GitHub push
is not deployment or release approval. Each unresolved gate stays open with its next
action in [current readiness](../BETA-READINESS.md) and the local Ops run receipt.
