# Private service mode (runtime phase)

The owner can switch new paid authorizations between the ordinary paid service and the
controlled free beta without editing code, rebuilding the site or restarting the API.

| Mode | Meaning |
| --- | --- |
| `paid` | Ordinary paid checkout for new, unselected customers. |
| `controlled_free_beta` | No **new** paid authorization may begin. Selected beta offers stay $0. |

This is a private operator action. There is **no web-exposed change endpoint** and no
customer-facing switch. The switch does not select a beta batch, accept terms, fund postage,
change prices, change beta eligibility, or activate live providers.

## What the switch does and does not touch

- **New paid intent:** `begin_authorization_attempt` reads the effective mode inside its own
  `BEGIN IMMEDIATE` transaction. Once a beta-on change commits, no new paid intent can be
  reserved; a stale caller boolean cannot override it.
- **Existing money:** existing holds, authorized, uncertain and linked paid cases keep their
  original amount, recovery path and reconciliation. Existing exact intents still replay.
- **Selected beta:** selected/accepted beta entitlements remain free and still refuse card
  authorization in either mode.
- **Enforcement is immediate** after the transaction commits. Open screens learn about it on
  focus/visibility return (and at most every 15 seconds while visible). A fresh page load
  always reads the current mode. Backend enforcement never waits for a browser refresh.

## Commands

Run from the backend package directory, against the **existing** database file. The CLI
refuses a missing path instead of creating a mistaken production database. The first open
after an upgrade may initialize the small phase table through the normal store migration.

```sh
# Inspect the stored override; normal migrations may run on the first open.
python -m parkingbreaker.service_phase_cli \
  --database /srv/parkingbreaker/data/parkingbreaker.sqlite \
  status

# Turn the controlled free beta ON for new paid intents (revision 0 = no prior change).
python -m parkingbreaker.service_phase_cli \
  --database /srv/parkingbreaker/data/parkingbreaker.sqlite \
  set --phase controlled_free_beta --expected-revision 0 \
  --command-id beta-on-2026-11-09 --actor Amir \
  --reason "Pause new paid authorizations"

# Turn ordinary paid checkout back ON. Use the revision printed by status.
python -m parkingbreaker.service_phase_cli \
  --database /srv/parkingbreaker/data/parkingbreaker.sqlite \
  set --phase paid --expected-revision 1 \
  --command-id paid-on-2026-11-12 --actor Amir \
  --reason "Resume paid checkout"
```

`status` prints JSON. With no stored override it reports `"stored_override": null`,
`"revision": null`, `"effective_phase": null` and
`"effective_source": "configured_fallback"`: the configured `PB_SERVICE_PHASE` is in force,
and the CLI does not guess it because it does not read the process environment.

## Safety rules

The durable checkout reads `PB_PAID_COHORT_CAP` from the backend environment. In live
mode an omitted value admits zero new paid appeals. Durable fake and test modes
can exercise any explicit nonnegative cap. The volatile preview cannot enforce one.
Use the same approved value on all writers of the same database. A phase override
to `paid` still respects the cap.

The limit counts distinct appeals over the lifetime of the transaction database.
The first committed payment intent reserves a place, including a declined or uncertain
attempt. Retry and recovery for that appeal reuse its place. An older paid case counts
even if it predates the attempt ledger. Closing a case, releasing a hold or issuing a
refund does not reopen capacity. Selected free entitlements use no paid places.
When full, the customer sees a saved-work message before card entry. If the last
place fills during checkout, the server refuses the new hold and refreshes the case.

- Every successful change is one append-only row with revision, exact phase, command ID,
  named actor, reason and UTC timestamp.
- `--expected-revision` must equal the current revision (`0` when none). A stale value
  refuses and writes nothing.
- The same command ID with the same original payload replays its saved result. Reusing an ID
  with any different payload refuses. Use a new command ID for each intended change.
- A missing or malformed phase table, a bad stored phase value, or a failed read fails
  closed. It never silently enables paid authorizations.
- Nothing here writes provider state. Only use temporary test databases for exercises; do
  not toggle the owner's working runtime as part of a test.


## Controlled-beta intake boundary

This phase switch pauses new paid authorizations only. It is not a complete intake
stop and does not stop existing free-beta entitlements. Do not select another beta
batch during an admission pause. Preserve original-intent reconciliation, refunds,
callbacks, customer status and support for existing work.

The target ingress rule for stopping all new intake still requires exact route
selection and a target rehearsal. Neither `PROCESSING_BLOCKED` nor switching the
service phase proves that rule exists. See [the controlled-beta operating packet](controlled-beta.md)
for target security, recovery, printing, notifications, support and device gates.

These commands are operating examples, not deployment or production-mutation
authorization. Do not copy the example database path or revision into a live action.
Verify the actual database owner, existing path, configured fallback and current
revision first. Run rehearsal commands only against an isolated synthetic database.
