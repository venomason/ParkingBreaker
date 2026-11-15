# Beta candidate — November 15, 2026

This is a locally verified candidate. Live beta is **not released or qualified**.
Deployment, provider spending, customer charges and mail require separate explicit
owner authorization. All browser campaigns below use synthetic providers/data.

## VERIFIED in the consolidated checkout

| Check | Result |
| --- | --- |
| Focused dispatch, freshness, official-form, upload and recovery regressions | 130 passed |
| `make check` | 13,635 backend tests passed; Ruff lint/format and strict mypy passed |
| `make web-check` | ESLint, TypeScript and production build passed |
| Controlled-free browser matrix, including ticket reading | 196 passed; four Chromium/Firefox desktop/mobile projects |
| Synthetic paid-journey campaign | 152 passed; no customer charges or mail |
| Offline research and image qualification harness self-tests | Passed; no application provider calls |
| Clean-checkout tooling regressions | 3 passed |
| Saved benchmark evidence | 109 of 109 quotations verified across 23 rows |
| Source parity | 625 application/frontend/deployment/backend-test files match source `996f35e` |
| History recovery | All 447 reachable source commits restored from the local bundle; full Git integrity passed |

The backend suite emits one dependency deprecation warning for Starlette's use of
`anyio.abc.BlockingPortal`. It does not fail the checks. Mobile browser projects
emulate viewport layouts; they do not establish physical-device or camera quality.
All 348 browser checks passed with no skips; all owned services and CONNECT proxies
were stopped after the campaigns.

Current intake supports JPEG/PNG images up to 10,000,000 bytes. PDF/HEIC inputs
remain explicitly unsupported, with recovery guidance. Server checks reject
corrupt/disguised inputs before persistence or downstream work. Customer review
and confirmation, approval/dispatch integrity, research freshness, and uncertain
prior-send recovery remain enforced.

## Remaining release work

**VERIFIED:** All 23 benchmark rows still have no owner signature. Quote matching
does not replace source adjudication or owner sign-off, and it does not qualify
model behavior against real providers.

**SOURCE-DERIVED from the archived project handoff:** The remaining release sequence
includes provider access/funding/privacy qualification under an exact approved cap;
qualified review of customer-facing copy and the clerical workflow; physical
iPhone/Android camera checks; target provisioning and recovery proof; Lob
print/proof/webhook qualification; operational coverage; and an explicit bounded
beta release decision. Account balances, credentials and cloud configuration were
not refreshed or changed during this consolidation.

**Next local action:** Review the signed consolidation branch and its publication
manifest. Historical worktree experiments remain preserved with their individual
next actions in the local `disposition.json`; they are not automatically promoted
to application changes. There is no failed local application check being waived.

Exact machine-readable validation is recorded in
`verification/consolidation-2026-11-15.json`. Full logs and recovery manifests live
under `~/Vault/Ops/runs/parkingbreaker-consolidation-20261115/` and are excluded from
publication. These receipts do not update historical task acceptance or grant
release authority.
