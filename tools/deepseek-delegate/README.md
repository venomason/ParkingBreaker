# Optional DeepSeek delegation helper

This retained helper is not the current coding-worker default. Follow [AGENTS.md](../../AGENTS.md) for worker selection and supervision. The provider/installation observations below are historical and do not authorize a call or imply current availability.

Verified November 15, 2026 UTC. This is a separate engineering helper, in `tools/deepseek-delegate/` in this checkout.
It does not modify the installed `deepseek-arm`, `dsh`, their profiles, credentials,
OS-maintenance services or the ParkingBreaker application. No automatic integration.

## Selected route and why

`delegate.py` adapts the installed direct arm's prompts, path guards, edit operations
and optional credential resolver. Provider transport is pinned to
`https://api.deepseek.com/chat/completions`; model is `deepseek-flash`, with thinking
disabled for these bounded mechanical tasks. No GPT, OpenRouter, plugin model route,
provider fallback, search/fetch tool, arbitrary shell command or worker subdelegation.
Request redirects and inherited proxy settings are refused/ignored respectively.

The native CLI was inspected using `dsh --profile headless --dump-config` (filtered in
memory; no raw configuration saved) and `dsh --profile headless --help`. It boots and its
composed default is `deepseek-official` / `deepseek-v4-flash`. The installed source says
it loads shared settings/credentials and tools, with default workspace-write permissions
that do not confine reads or networking. Its one-task CLI exposes no turn or cost flags.
It was therefore not selected for automatic bounded packets; no live native-headless
model call was made, and this does not qualify every native harness feature.

Local references: `Projects/deepseek-harness/apps/cli/reference/README.md:81`,
`packages/bundle/headless/src/index.ts:105`, and
`~/.local/lib/deepseek-arm/dispatch-deepseek.py:157,288,383,437`.
The wrapper removes the original direct arm's arbitrary command tool and automatic
request retries; it adds exact file lists, evidence, snapshots and finite limits.

## Credentials and effective model

Default `--credential-source harness` reads only `refs.DEEPSEEK_API_KEY` from
`~/.dsh/.credentials.yaml` into broker memory. Its owner-only permissions were verified,
and authenticated live calls succeeded. The file is never copied, printed or supplied to
worker tests. Account/session tokens in that document are not used. No credential is
written to the environment, directive, workspace, receipt or command line.

`--credential-source arm` explicitly selects the original arm resolver (environment,
then `~/.grok/secrets.env`). Its stored key returned HTTP 401 on the first canary.
It differs from the working harness-managed API key. There is no automatic credential
fallback or repeated authentication loop. The original failed receipt is retained.

Successful responses report `model: deepseek-flash`. The provider's current
[model documentation](https://api-docs.deepseek.com/quick_start/pricing/)
maps that name to DeepSeek-V4.1-Flash-1017; this internal version is provider-documented,
not independently attested by the response. It also describes the older V4 Flash name
as a compatibility alias. Requests use the official API and its API-credit billing;
no GPT API call is present. Remaining account balance and actual invoice charge were
not inspected; token usage is recorded from responses.

## Exact invocation

From this directory, inspect a packet first with a dry run (no credentials/API used):

```sh
python3 -B delegate.py \
  --source examples --packet examples/audit.json \
  --out /tmp/pb-deepseek-audit-dry-UNIQUE
```

Run an authorized bounded investigation or edit:

```sh
python3 -B delegate.py \
  --source examples --packet examples/edit.json \
  --out /tmp/pb-deepseek-edit-UNIQUE \
  --credential-source harness --live
```

Real ParkingBreaker investigation (already exercised against the current file):

```sh
python3 -B delegate.py \
  --source ../../parkingbreaker-fresh \
  --packet examples/parkingbreaker-investigation.json \
  --out /tmp/pb-deepseek-investigation-UNIQUE \
  --credential-source harness --live
```

Each output path must be new and outside the source tree. The worker receives private
copies of **only** `context` files. Existing writable files must appear in `context`;
new paths may appear only in `writable`. Use a JSON packet with exactly these fields:

```json
{
  "mode": "edit",
  "objective": "One explicit change; expected behavior and stop condition",
  "context": ["module.py", "check_module.py"],
  "writable": ["module.py"],
  "tests": {"unit": ["/usr/bin/python3", "-B", "check_module.py"]}
}
```

For read-only work use `mode: audit` and `writable: []`. `read_file` returns the exact
excerpt, original relative path, one-based line range and full-file SHA256. Ask for a
short report with citations and explicit uncertainty. Model summaries are claims;
`tool-evidence.json` and actual bytes are the evidence.

## Responsibility and integration

Astra retains architecture, scope, interpretation of policy/source evidence, test design,
trusted checks, integration and acceptance. Delegate narrow code tracing, inventories,
mechanical edits, focused test additions and diagnosed fixes. Split large investigations
into packets with exact files. Do not delegate financial/legal decisions, credential
repair, frozen-guard exceptions, release authorization or final acceptance.

1. Read current project instructions/diff and identify active writers before dispatch.
   Use one task per dependency chain. This adapter serializes its own runs with a local
   lock; it does not lock other editors or unrelated harness jobs.
2. Prepare the allowed context and trusted evaluator. Review it for secrets and customer
   data before any API use. Hidden paths, credential-shaped content and symlinks are
   refused, but that heuristic is **not a comprehensive privacy scanner**.
3. Inspect `receipt.json`, `changes.patch`, `tool-evidence.json`, and `workspace/`.
   Verify `source_unchanged` and `scope_ok`; compare baseline hashes again immediately
   before integration. A concurrent change requires rebase/review, never overwrite.
4. Rerun trusted checks independently. Keep frozen evaluators read-only and separately
   hash any worker-authored tests. `worker_finished` is not supervisor acceptance.
5. Review and apply the minimal diff deliberately; this tool has no apply/commit command.
   Record accepted/rejected/blocked and the next action in ParkingBreaker's existing
   checkpoint/evidence records. Run broader checks once at the integrated milestone.

Files such as `research/findings.py`, `support.py`, `procedure.py`, and
`route_requirements.py` may be read as context but must not become writable without the
project's reviewed exception. Existing source gaps and readiness gates still apply.

## Enforced isolation and limits

- File tools accept exact packet paths, with no absolute paths, traversal, symlinks,
  hidden paths or arbitrary reads. Audit mode has no edit authority. Writes affect only
  the disposable copies; no Git history or `.env` is copied.
- The worker can choose only supervisor-defined test names. Tests execute under
  unprivileged `bwrap` with separate PID/network/user namespaces, readonly `/usr` and
  readonly snapshot, fresh `/tmp`, clean environment and no host home or service sockets.
  Even worker-edited test code cannot change its snapshot. Actual isolation probes
  verified denied host-home access, denied workspace writes and failed external connect.
- Commands are limited to 30 seconds wall/CPU, 2 GiB address space and 1 MB captured
  output (16 KB model excerpt). Tools cannot install packages or access external services.
  The broker alone has the DeepSeek credential and HTTPS connection.
- Six model requests maximum; 1,536 output tokens/request; 300 seconds overall; no API
  retries. A failed/ambiguous call records possibly unreported usage. Context files total
  at most 160 KB; each serialized model request at most 100 KB; reads at most 200 lines.
- A local $0.05 conservative estimate admission limit uses peak Flash rates and reserves
  input bytes plus overhead/output before another request. It is **not** an account-level
  or provider-guaranteed billing cap, and does not change financial settings. Recheck
  the cited prices before extending usage. No calls are made without `--live`.
- Published provider concurrency is much higher; this helper deliberately uses one
  worker. See [provider limits](https://api-docs.deepseek.com/quick_start/rate_limit/).

Limitations: currently named checks support system Python 3 or system Node only; full
ParkingBreaker Python dependencies and nvm/Next builds are not mounted. For those tasks,
delegate a small implementation and run the project's trusted checks as supervisor.
Do not claim missing dependencies passed; add an explicitly reviewed readonly dependency
mount if needed later. Oversized files/tasks need narrower packets. Provider/model
aliases can evolve; response identity and usage are retained for every request.

## Actual examples and verification

`evidence/audit/`: DeepSeek read a deliberately faulty whitespace normalizer, cited both
source/test hashes and lines, and ran the frozen test to an expected assertion failure.
No source writes. The worker overstated the risk of a fixed test-count print; supervisor
review does not accept that as a defect. This is why worker prose is not final judgment.

`evidence/edit/`: DeepSeek changed only
`return text.lower().replace(' ', '-')` to `return '-'.join(text.lower().split())`.
The frozen evaluator passed and Astra independently reran it in the sandbox. The diff
is accepted for the disposable demonstration only; no production change was integrated.

`evidence/investigation/`: bounded read of real `research/workflow.py`, with exact
excerpts/hashes, checked by Astra against current bytes. The first attempt's response
used normal final text and the adapter expected a `finish` call; that failed receipt
and usage remain in `evidence/investigation-initial/`. One diagnosed correction now
accepts normal final text after tools, still pending supervisor acceptance.

Run offline enforcement tests with `python3 -B -m unittest -v test_delegate`.
`verification.json` records final checks, totals, preservation and artifact hashes.
Existing installation/profile/configuration hashes are recorded in `baseline.json`;
credentials themselves are excluded. No OS-maintenance configuration changed.

Observed setup usage (including the diagnosed correction): 11 successful model responses,
15,455 input tokens, 4,423 output tokens, including 8,576 cached input tokens. Counting all
input at the higher noncached peak rate gives **$0.0099441** as a conservative estimate;
actual billing was not queried. One additional request returned 401 without usage.
See `evidence/usage.json` and per-run receipts. Successful disposable tests qualify this
bounded route, not arbitrary long tasks or unattended paid campaigns.

The real-source report contained line-reference/field-name errors in prose. Astra checked
the raw excerpts and recorded corrections in `evidence/investigation/supervisor-review.json`.
Use those corrections; never treat the model's prose as mechanically validated citations.
