# NEOpb / ParkingBreaker engineering workflow

Owner direction: November 17, 2026 (Pacific). This is the shared project workflow for every coding model. CLAUDE.md and GEMINI.md are thin entry points, not separate policies. Follow higher-priority instructions and the owner's current task; current owner direction supersedes older project workflow preferences.

## Direct execution and efficient context

- The active model codes directly and owns design, implementation, verification and completion. This replaces the November 16 mandatory AGY/Hydrogen workflow and older DeepSeek default. Do not switch providers, launch workers or add model handoffs automatically.
- Unless the owner explicitly requests delegation for the current task, it requires demonstrated at least 10× lower total token use on comparable work, including coordination, worker context, review and retries, with no reduction in quality or completion speed. No such comparison is established here. Do not run paid benchmarks just to justify delegation. Report measured results; never invent savings.
- Inspect the worktree status and relevant diff first. Read the next task in docs/ROADMAP.md and its named source/tests. Use targeted rg searches and bounded reads; expand only when a dependency or uncertainty requires it. Do not load source exports, dependencies or entire historical logs for a narrow change.
- For substantive changes, establish the observed defect or requirement, intended behavior, affected files, invariants and acceptance checks before editing. Keep this brief; do not create another planning document when the roadmap already owns the task.
- Batch independent reads/checks where safe. Keep dependent writes and stateful operations sequential. Reuse verified context until relevant inputs change. Keep progress updates concise and focused on findings, remaining uncertainty and next action.

## Implementation and verification

- Preserve unrelated and uncommitted work. Make the smallest coherent change that solves the cause; do not weaken behavior, add speculative abstractions, rewrite working architecture or delete files merely because they look old.
- Proceed through authorized reversible work without repeated permission questions. Ask only for a material missing decision that cannot be established from current code or connected evidence. Respect tool approval gates; a denied action is not permission to retry it through another tool.
- Extend the authoritative transaction store, case history and evidence-backed financial ledger. Keep milestone writes atomic with their state changes. Failed attempts need durable evidence of their own; never fabricate a success after rollback. Reconcile uncertain external effects before retrying them.
- Preserve frozen text/PDF hashes, customer approvals, authorization amounts, mail/capture rules and official-source requirements. Telemetry or experiment changes cannot alter existing commitments. Keep raw customer content, bearer tokens and payment identifiers out of analytics, Slack and model diagnostic bundles.
- Use existing test conventions and commands from README.md, Makefile and package configuration. Add regression coverage for meaningful new behavior or failure modes. Run focused tests first; include relevant recovery, atomicity and integrity suites for shared storage, lifecycle, authorization, money or mail changes.
- Run required lint/type/format checks for affected code. Run full release gates on a stable candidate, not after every documentation edit. Repeat checks when changes, failures or unresolved concerns justify them. A Markdown-only edit normally needs diff, link and consistency checks, not the application suite.
- Inspect the final diff. Do not weaken tests, suppress unexpected failures or accept a worker's success report as proof. If delegation is explicitly authorized, bound writable files and stopping conditions in an isolated checkout, then independently verify the result.

## Telemetry, launch and completion

- Quantify meaningful journey decisions, failures, retries, recovery, fulfillment, marketing and settled financial outcomes. Preserve unknowns, actual exposure, timing basis and missing capture. Client observations and AI diagnoses are not authoritative business facts.
- Durable events and a persistent queue initiate diagnosis without Amir relaying incidents between chats. Slack displays timelines, business results and repair status; it is not the transaction authority. Existing evidence-backed automatic remedies keep their existing controls.
- docs/ROADMAP.md owns remaining work; docs/BETA-READINESS.md owns release evidence. Keep implemented, tested, configured, live and measured distinct. Local synthetic success does not establish live providers, production health or profitability.
- Finish with what changed, exact checks/results, material limits and the next task. Record reproducible evidence for substantive milestones; update canonical docs rather than adding stale handoffs or duplicate roadmaps. Never claim an action, test, deployment or financial outcome that was not observed.
