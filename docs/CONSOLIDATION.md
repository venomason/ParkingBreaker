# November 15, 2026 consolidation

The active checkout is `Desktop/ParkingBreaker-Codebase`, backed by
`https://github.com/venomason/ParkingBreaker`. Work is prepared on
`beta/consolidate-2026-11-15`; publication requires the owner's exact push approval.

## Provenance and integration

- GitHub base: `13042776582aeecc3b516f8b5ea02aff8965f97c` on `main`.
- Projects source: `996f35ee8839dc104ada29be5f9d0b2c806dc03e` on
  `feature/cro-landing-redesign`, with no remote at inspection.
- The GitHub base tree exactly matches the Projects `github-codebase` tree:
  `34893fce9000339d0818e8a4c08d1c4e118d57af`.
- Later application work comes from `554d6d812339c9fd7704f5c1926d3d44002c497d`
  and `d099876d624147188f77f9ae34e9f444d6abe554`; `996f35e` adds the handoff.
- At inspection, 761 shared tracked files were identical, 37 differed, and there
  were no GitHub-only tracked files. The source also held 12,196 extra tracked
  files, mostly historical evidence, operational state and archives.

The integration preserves current dispatch validation, research freshness and
uncertainty handling, official-form line interpretation, corrected benchmark keys,
image upload/recovery behavior and regression tests. It retains the clean
checkout's GitHub-specific README, Makefile and exclusion policy, updated for this
consolidation. Qualification scripts and synthetic fixtures now live under
`tools/qualification`; their self-tests make no provider calls.

Two public HTML captures contained embedded browser API-key strings. The new
branch imports their extracted text as supported `<raw-sha256>.txt` sidecars,
preserving quote verification without publishing those raw captures. The raw bytes
remain in local historical evidence. Other reference captures preserve their exact
hash-addressed bytes; `.gitattributes` prevents text normalization.

Clean-checkout validation also exposed two portability defects. `make sync` now
selects a compatible system Python and verifies the existing Linux OFD-lock
requirement. `kit.py run-check` no longer loads archived task/progress state it does
not use. Governance commands still require that state. Dedicated regressions cover
receipt/exit behavior, invalid check IDs and missing governance state.

The native DeepSeek Harness was attempted for the bounded tooling fix but rejected
its configured credential before changing files. The supervisor completed the
announced small correction and ran the trusted checks. No credential repair or
alternate worker-provider dispatch occurred.

## Preservation and exclusions

Before consolidation, 12 local archives were hashed and verified against their
member manifests; two additional historical copies received verified archives
before their later relocation, bringing the total to 14. A separate verified Git bundle preserves all 447 reachable
source commits and refs. Original history is retained locally instead of being
attached as unrelated ancestry to the publication branch. The GitHub `main` branch
and its history remain unchanged.

Local run records are under
`~/Vault/Ops/runs/parkingbreaker-consolidation-20261115/`, including:

- `backup-receipts.json`, `bundle-receipt.json` and file manifests;
- `repository-inventory.json` (184 repository/worktree candidates);
- `integration-plan.json` and `publication-exclusions.json`;
- `move-manifest.json`, `move-verification.json` and `disposition.json`;
- focused, release, qualification and browser check receipts.

The source's seven untracked files remain preserved: six successful generated
frontend-check receipts/logs and one whitespace-only editor scratch file.
Credentials retain their original paths through the legacy alias and original
permissions; they were not copied into this checkout. Local databases, dependency
environments, exports, private operational records, worker receipts and historical
screenshots are excluded from publication. Filename-sensitive backup exclusions
are recorded explicitly; originals remain intact, and tracked source is also
recoverable from Git history.

The existing GitHub baseline already includes synthetic credential fixtures and
public webpage captures with embedded browser API-key strings. No new credential
material is added; the publication scan distinguishes those unchanged baseline
objects from newly introduced files. No remote history rewrite is proposed.

## Legacy mapping

All moved directories are beneath
`~/Projects/ParkingBreaker/LEGACY/consolidated-2026-11-15/`:

| Former location | Legacy directory |
| --- | --- |
| `Projects/ParkingBreaker/parkingbreaker-fresh` | `projects-source-996f35e` |
| `Desktop/ParkingBreaker` | `desktop-session-start-backup` |
| `Desktop/ParkingBreaker-Claude-Review-2026-11-13` | `claude-review-2026-11-13` |
| `Workspace/parkingbreaker-core-20261016-cBq6J7` | `workspace-typescript-core-2026-10-16` |
| `Documents/ParkingBreaker` | `documents-build-plan-2026-10-27` |

The former Projects source path is a compatibility symlink to its legacy
directory. This preserves 160 shared Git/worktree references and absolute
dependency links without rewriting archived metadata. It is not an active
development destination. HEAD and dirty-state comparisons passed after relocation.
Readable process handles/mappings were checked before moving; the owner confirmed
there were no other active writers.
The existing `Projects/parkingbreaker-fresh` alias also resolves to the preserved
legacy source. The supplemental home inventory found historical Codex session
artifacts and an already labeled `Backups/ParkingBreaker-2026-10-27` recovery set;
these remain in place, with manifests and dispositions recorded locally.

Managed `.codex/worktrees` and `.local/state/pb-wf` copies remain in place, backed
up and inventoried. Some contain unique experimental or dirty work; those changes
are preserved rather than implicitly accepted into the application. In particular,
alternative webhook benchmark implementations remain experiments requiring a
deliberate integration decision. Existing legacy archives and temporary test stores
remain intact. Ten pre-existing missing/unborn-HEAD states were recorded; none was
silently repaired or discarded. Per-folder next actions are in `disposition.json`.

## Recovery and publication

Use `move-manifest.json` to reverse directory renames, accounting for the one
compatibility symlink. Restore archives into a new empty directory, never over a
working checkout. `fresh-all-refs.bundle` is a standalone local Git recovery source;
do not mirror or push its historical refs. The original legacy directories remain
available alongside the backups, including protected local files omitted from
archive copying.

The proposed push creates the named review branch only. It does not update `main`,
force-push, deploy, configure providers, charge customers or mail anything.
See `BETA-READINESS.md` for validation and the remaining release gates.
