# Controlled beta operating packet

Prepared November 15, 2026 from application commit
`e4e4a28a912c84c34e66044f9e47ae7aba3d6b4b`; evidence updated November 16.
See [current readiness](../BETA-READINESS.md) for source-specific results.
Status: **NO-GO for live intake**. This is a preparation and acceptance packet,
not permission to provision, deploy, use paid providers, send notifications or mail.

## Current owner direction (November 17)

The planned beta is paid. Use the existing paid authorization, exact-packet approval,
mailing and capture controls; do not select a free entitlement batch for launch. Historical
free-beta qualification below remains historical evidence only. New intake requires ticket
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

## Configuration and provider access

Use the reviewed `.env.example` as a **nonsecret template**, not the archived `.env`.
The example selects `fake`, `paid`, DeepSeek V4.1 Flash intake
(`deepseek-flash`) and the existing DeepSeek research/drafting roles. The optional
Gemini packet-check preset is retained; the current customer-authored service does
not construct a packet-check model. `PB_LLM_PRESET` must be absent:
even a blank value overrides role selection. Unused support/digest preset fallback
code still names MiniMax; it is not selected by these four role settings.
Changing the example does not change the running service or grant beta entitlements.

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
VM overlay is already selected by `deploy/lifecycle.sh` with
`PB_DEPLOY_TOPOLOGY=cloudrun-private-vm`; its older introductory comment predates
that integration. Offline lifecycle tests cover the overlay. They do not qualify
actual network isolation, disk fencing, IAM, TLS or notifications.

Use the existing lifecycle after exact approval, with its validation, exclusive
writer, snapshot, migration/compatibility, health and readiness gates. Do not bypass
it with an ad hoc `compose up`. No cloud creation command has been executed here.

## Intake stop and incident handling

These are different controls; record which one an incident needs.

1. **Stop new paid authorizations:** the existing private service-phase CLI stores
   a revision-checked override. It does not cancel existing holds, replay uncertain
   sends, stop admitted free cases or close every intake route.
2. **Stop new free-beta admission:** do not select or approve another entitlement
   batch. Existing admitted cases retain their recovery and support obligations.
3. **Stop all new intake:** prepare a target ingress rule for the precise new-intake
   routes, preserving customer status and provider callbacks where feasible. This
   rule is not provisioned or qualified. The service-phase switch alone is insufficient.
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

## Recovery and rollback

The executed local recovery rehearsal uses only synthetic storage and fake providers.
It proves backup/restore record and artifact equality, quarantine, original-intent
reconciliation, and no second fake mailing after three dispatch attempts. It is not
an off-site or production restore qualification.

For the actual approved target drill: back up using `parkingbreaker.recovery_cli
backup`, verify the manifest and off-host copy, restore into a new isolated directory,
keep `RESTORE_QUARANTINE.json`, prove the original host is fenced, and reconcile the
original-account Stripe/Lob IDs before unquarantine. `recovery_cli reconcile` currently
reports `NEEDS_EXTERNAL`; it does not implement an automatic live reconciliation.
Never restore over the current database or remove quarantine to make startup pass.
Rollback must retain authority data and pass compatibility on a copy; otherwise roll
forward. A `PROCESSING_BLOCKED` lifecycle marker is not a global intake kill switch.

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

Do not release until provider, target, printing, notification, human and physical-device
receipts identify the exact candidate and pass. Specify the start/end instants, funded
cap, one batch of at most five entitlements under the existing admission rules,
intake-stop owner and explicit activation authority. A GitHub push is not deployment
or release approval. Each unresolved gate stays open with its next action in
`../BETA-READINESS.md` and the local Ops run receipt.
