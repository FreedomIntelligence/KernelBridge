# Harness 20 model-turn optimization log

## Interpretation of the recorded experiments

The completed clean trials used KernelBridge around Pi 1.5.0 with the `ds`
route and the configured model `deepseek-flash`. An `openai` provider adapter
describes the API protocol, not the model vendor. These are not GPT-in-Codex
trials. Comparisons with earlier Codex work change the model, framework, prior
context and call allowance together and cannot establish their separate effects.

The initial bounded trials used 20 model admissions shared by parent, child,
compaction and review, not 20 source-edit/device-experiment iterations. Later
uncapped trials follow the user's removal of aggregate turn and token limits;
their actual calls and usage remain recorded. Model reasoning and
candidate generation, the generic agent runtime, our domain tools, and CANN
compilation/profiling have different responsibilities. Improvements in prompts,
source, logs and knowledge resources are not evidence of model-weight updates.

For Cube, candidate freeze at call 17 and launch at call 18 show late model
preparation; collection at call 19 without the concrete compiler error and F
feedback after call 20 show an additional feedback-delivery cost. Both must be
measured separately. Missing captures before a runnable kernel exists do not
measure the model's ability to interpret available IR or profiler evidence.

## Harness feedback repair after main synchronization

The working branch incorporates `main` through `5af7700`, including its TUI,
trace, queued-input and interruption-feedback changes. This user-requested functional repair bundles several mechanisms; it
is not a single-variable optimization experiment and has no new device speedup
claim. Original clean trials and their frozen profiles remain unchanged.

- `pipeline_evaluate` composes D/E/F under one admitted tool action. Internal
  phases retain their own immutable intents, result manifests and original
  handles. Bounded pending calls resume the same identity; committed checks are
  reused when source bytes are unchanged, including a zero-wait cache read.
  An interrupted F or diagnostic blocks new effects through both the composed
  and legacy tools. Recovery of an orphan completed F also validates its original
  Journal admission and source/evaluator context before host acceptance.
- Native terminal feedback now includes the first original compiler diagnostic,
  its original location and hash-bound read links during E. Generic check errors
  retain a bounded excerpt and the full original F receipt. Large projections
  keep actionable feedback; paging reports encoding, size, hash and EOF without
  replacing split UTF-8 characters. Diagnostics are a sibling envelope, leaving
  original F/G result bytes unchanged for strict acceptance.
- Active Goals expose an observed, protected Journal call-count snapshot before
  model input is committed. Its remaining count includes the current request;
  concurrent admissions may consume it. Parent, child, compaction, review and
  failed requests share the original counter. Completed/inactive Goals do not
  impose stale progress on ordinary codegen. Missing caps stay absent. Failed
  provider usage retained in `failure_details` is counted by the original ledger.
- Newly prepared NPU profiles capture candidate source through both freeze and
  evaluate, and project original diagnostics on F failure or G regression by
  default. Explicit boolean switches can opt out or require diagnostics. These
  projections do not rerun device profiling; unavailable captures remain
  unavailable. The oracle, cases, eager source, measurement and noise gates are
  unchanged. Existing sealed profiles require a new preparation to gain the tool.

Functional validation includes actual local child-process interruption during
E and F, original-handle recovery with dispatch count unchanged, unchanged-source
reuse, complete F/G/H-to-host acceptance with protocol fixtures, tamper rejection,
and offline replay of the original Max/Cube compiler failures. These are runtime
and evidence-contract checks, not real NPU qualification. The local TUI checks
cover parallel/background children, normal codegen, compact/new, interruption,
Goal opt-in, provider failure recovery, resume and trace CLI/SSE. This repair's
codegen smoke uses three ordinary turns; the earlier long-run evidence is separate.
Browser rendering was not exercised in this run because Playwright was absent.

Qualification logs and hashes are indexed in
`.validation/harness-feedback-qualification.json`; the initial failed API
assertions and corrected reruns remain alongside it. Reaching correct native
execution and a measured above-noise improvement still requires fresh NPU trials
after transport and original deployment identity are recovered.

### Rebase qualification against main `ae59e4f`

The branch was rebased onto `ae59e4f`, retaining the independent harness repairs
from both previous merge commits. The product TUI layout is byte-identical to
that main revision, including its width-dependent footer priorities and the
standalone Pi trace plugin. The operator examples, worker and executor sources
remain byte-identical to the previously qualified `b31b9bb` snapshot. The user
subsequently authorized a direct fast-forward update of `main`, contingent on
checking that its remote tip has not advanced. The original remote feature branch
is preserved; no force push is needed.

Post-rebase validation passed 53 TUI Python tests, 71 targeted pipeline/receipt
tests and 114 Node tests with type checking. The two optional Node replay cases
were separately exercised with valid original artifacts in a 15-test replay run.
Actual terminal scenarios passed 10 interaction checks, eight parallel/background
checks and 10 ordinary-codegen checks (three ordinary turns, 16 local fixture
requests). Parent inputs complete while a child remains running; completion
notifications retain their system identity and can continue the parent; Esc
still permits the next input. The initial two PTY failures used old footer-text
assertions. Only their fixtures changed: detach policy is checked at a wide
width, and the narrow footer and background status follow the remote layout.
Original failures and passing reruns remain under `.validation/rebase-ae59e4f/`.

These are local integration checks and original-receipt replay, with no new model
trial or NPU performance result. The resumed optimization goal still requires
recovery of the original deployment identity before fresh device trials.

### Resumed preflight: fresh inputs, no new trial

A read-only re-audit of all 16 formal Journals verified their hash chains and
matched every frozen index head: 303 model calls, 5,713,968 known tokens and zero
accepted E3 trials. This is an evidence check, not another optimization run.

The unstarted `include-roots-*` inputs cannot test the current repair unchanged:
they register ten tools without `pipeline_evaluate`, retain old driver pins and
runtime bindings, and disable failure/regression diagnostics. Those original
inputs remain untouched. New local inputs are sealed as
`.validation/harness-trials/feedback-d25875b-{max,min,matmul}-01`, with eleven
tools, current runtime/driver bindings, the shared 20-call ceiling and diagnostic
defaults enabled. Each preparation verifies unchanged contract, cases,
measurement, oracle and baseline hashes; none contains a candidate, session
history, attempt or dispatch. These prepare steps made zero model, transport,
upload or device calls. Their equivalence and cleanliness evidence is indexed in
`.validation/rebase-ae59e4f/fresh-trial-readiness.json`.

Both configured access paths remain unavailable on the resumed read-only check:
SLAI method negotiation times out and `hetao-web` refuses the SSH connection.
The original `cc39657de0d9bbcfd94e8589b69813ddd1f9c9167670c85719852422168244f2`
deployment remains unknown. The new manifests explicitly record that their
local preparation does not verify or update this remote payload. Before launch,
recover that original identity and reconcile the deployment with the required
runtime closure; do not substitute a namespace or repeat an unknown upload.
No new correctness, timing, IR/profiler capture or promotion result is claimed.

### Restored deployment and first real feedback trial

The authorized Hong Kong route successfully reached the original SSH target.
Read-only recovery proved the incomplete deployment namespace was absent and
the old jobs were terminal. Exactly one upload of the original frozen payload
then completed; an independent readback matched all 304 file hashes and the
original deployment identity. The remote execution closure (26 files including
package initialization, templates and runtime settings) is byte-identical to the
current required code. Updated host-only files did not require replacing that
remote payload. Evidence: `.validation/hongkong-resume-20261111/`.

The original node's device 0 was occupied by another workload. A separate fresh
device 1 contract was prepared after checking its physical mapping and idle
state. Case, oracle, baseline, warmup, repetition and timing settings were
unchanged; the device index and corresponding contract/environment binding were
explicitly changed. Device 0 calibration and timings cannot be reused. The other
identified node was busy, and a third endpoint required a different SSH identity.

`hongkong-device1-feedback-max-01/run` completed with 20 parent model calls,
657,244 reported tokens, no unknown usage, no child/compact/review calls and
`budget_limit`. All 20 protected call-budget snapshots matched the authoritative
Journal. The agent wrote its only candidate at call 7 and used the new composed
evaluation at call 8. This is a real clean model trial, not a protocol fixture.

It did not reach compilation: the model's declared contract added `baseline` and
`candidate_abi` fields. Strict native validation rejected it locally before any
trial/transport invocation, but the pipeline treated the nonzero adapter exit as
an unknown launch. The composed result had no direct error feedback and suggested
recovery of a null handle. The model read the original stderr at call 11; the
specific validation error first entered its next request at call 12. Rebinding
and reevaluation could not clear the original unknown-effect fence. The original
Journal, failed contract and candidate remain intact. No F, G, H, IR/profiler
capture or measured speedup exists for this trial.

**Next hypothesis:** a pinned adapter can prove a strict local preflight rejection
before any dispatch, return concrete contract differences in the same evaluation
call, and permit correction without falsely entering unknown-effect recovery.
This must use a versioned, exact request/source/driver-bound receipt, not null
handles, missing attempt files or matching stderr text. Real transport uncertainty
and post-dispatch failures must retain their original fence. The change applies
to arbitrary task contracts, including Cube; it must neither trim extra fields
nor weaken oracle/measurement equality.

### Pre-dispatch rejection feedback — implementation qualified locally

The native launch adapter now emits `kernelbridge.driver-preflight/v1` only
for a strict contract mismatch before its durable dispatch boundary. A pinned,
explicitly opted-in driver returns the original request/source/binding identity,
bounded JSON-pointer differences, and complete hash-bound expected/actual/diff
artifacts. No extra contract fields are discarded. The pipeline commits and
revalidates the receipt and all retained evidence before excluding the intent
from unknown-launch recovery. `pipeline_evaluate` returns failed preflight
feedback immediately, without collection, F or diagnostics; the next action is
to correct the declared contract. Timeouts, malformed or unsupported replies,
changed inputs, post-boundary failures and incomplete commits remain unresolved.

Local qualification passed 91 distinct tests: 20 new native/integration tests
and 71 existing affected regressions. The optional original Max/Cube replay was
explicitly supplied both retained trials and passed. The first old-regression
invocation omitted the validation directory from PYTHONPATH, causing four import
errors; those modules passed after correcting the invocation. Initial new-test
failures were fixture errors, not production changes. Actual subprocess tests
cover contract correction with exactly one subsequent fixture dispatch, request
and intent rewriting, false collector handles, committed artifact tampering,
unknown F/J fences, complete Unicode evidence and generic Cube contracts. The
original failed max request was replayed read-only with zero transport calls.
Logs: `.validation/preflight-rejection-repair/`. These tests do not establish
real NPU correctness, timings or diagnostic capture; fresh trials follow.

No aggregate token limit is configured. The 657,244 tokens above are reported
usage, distinct from the explicit 20 model-admission ceiling. Existing model
settings retain their per-response output parameter for trial comparability;
this is not a total session token budget.

## Goal and accounting

The active goal supersedes the earlier request for 20 externally coordinated
candidate attempts per operator. A trial now starts a fresh `./kernelagent`
session with only task.json and an NPU profile, no previous conversation and no
prewritten or retained candidate. `application.max_model_turns = 20`; semantic
compaction counts, and child model calls/tokens are reported separately.

Tasks: FP32 feature-index scatter max, scatter min, and one distinct CANN task
selected below. The scatter semantic oracle, six-case suite, tolerance and
measurement settings remain frozen. The baseline is the full-NPU composition in
`examples/scatter/npu_eager.py`. CPU fallback does not qualify. A successful trial
needs complete source-bound F correctness, same-device eager ABBA improvement
above measured calibration noise, and successful promotion. Any regressing case
or family is reported. E4/platform acceptance is outside this goal.

Only one device job runs at a time unless the profile explicitly permits more.
Unknown dispatches are recovered using the original handle and never resubmitted.
Harness changes must transfer beyond scatter; no candidate implementation,
optimization answer or case-specific shortcut is added to prompts or configuration.

## Workspace and starting evidence

- Main was pulled at `db57826` before this goal. Its earlier redesign is already
  merged upstream. New work is on `codex/harness-20turn` in the managed
  `harness-20turn` worktree.
- The preceding unfinished implementation was copied with its original tracked
  diff and new-file hashes in `.validation/migration/`. The shared checkout was
  left intact because another session has edits there; it is no longer modified
  by this goal.
- Existing scalar source probes, supplied candidates, local fixtures and resumed
  conversations are **not** clean baseline trials. Formal goal trials: **0**.
- Native max/min preflights passed six cases each; the configured CANN 9.2 compiler
  rejected IR export flags, which is retained as unavailable. Real NPU profiler
  output exists. These establish transport/tool availability, not goal success.

## Pre-trial functional qualification

The inherited redesign includes parallel/background children, real command
cancellation, bounded streaming, native regular TUI, notification recovery and
opt-in A-J workflow tools. Targeted tests and several PTY flows passed. The final
Esc cancellation regression exposed a settled-permit race; this must be repaired
and the affected PTY rerun before claiming complete functional qualification.

Actual DeepSeek exercised A-J with local protocol fixtures. Those fixed timings
are not performance measurements. A runtime-version handoff and observer timeouts
mean these attempts are retained as partial acceptance evidence, not clean trials.

## Trials and improvements

No formal trial has run. First freeze the qualifying harness baseline and the
task/profile contracts, then run one clean baseline trial per task. Record exact
model turns, tokens, child usage, original runs/receipts, correctness, all paired
case results, measured noise and promotion. Choose the largest observed waste
category before changing one general harness mechanism. Keep failed attempts and
repeat successful clean trials to establish reproducibility.

| Iteration | Hypothesis/change | Task/trial | Turns/tokens | E1 / E3 / promotion | Conclusion |
| --- | --- | --- | --- | --- | --- |
| Baseline | Pending functional qualification and clean task setup | Not started | — | No goal evidence yet | Pending |

### Qualification: clean task entry and receipt completion

Before the first baseline trial, the existing `--profile` entry only accepted the
CANN task format with a prewritten six-source template. Direct codegen settings
also did not forward `application.max_model_turns`, and pipeline H had no
application acceptance binding. These are measurement setup gaps, not a tested
optimization-prompt improvement. The generic operator-task/profile entry now
seeds only declared readonly contract/ABI documents into an isolated workspace;
writable candidate sources begin absent. Application limits are forwarded, and
completion requires the original admitted F/G/H, immutable oracle/baseline, same
source snapshot, full suite, and calibrated positive pairing. The total model
call ceiling remains unchanged: parent, compaction, child, and review all count;
separate role counts/tokens are telemetry, not extra allowance. This applies to
any declared operator contract, not only scatter. Qualification tests are pending.

### Startup qualification failure (zero model calls)

At frozen commit `8410053`, `--goal-from-task --on-detach continue` created an
active Goal but no model call. The on-detach control receipt made the TUI's
initial-input guard believe a task input already existed. The observer recorded
zero model calls and zero device jobs in
`.validation/harness-trials/baseline-max-01/run`. That session was paused and its
CLI closed; it is a startup preflight failure, not a completed baseline trial.
The generic guard now ignores control-only history while still preventing
replay of a queued/applied task. A regression exercises exactly-once initial
submission after a control receipt. This is functional readiness, not a claimed
optimization improvement. The fresh performance session uses a new run directory.

Local qualification: Node 65/65 and TUI Python 35/35 passed before this startup
fix; the new startup regression and existing settlement/observer tests pass 3/3.
The full Python discovery ran 162 entries: 155 passed, two skipped, and five
imports lacked Torch in the strict runtime environment. Those five are being
rerun with the existing local Torch test dependencies; no packages are installed
or device results substituted. Enhanced parallel PTY passed all eight checks,
including foreground independence, original background notifications, Esc
cancellation, and immediate next input.

### Baseline max 01 — completed, unsuccessful

- Harness: `c5fe484`; fresh session under
  `.validation/harness-trials/baseline-max-01/run-02`.
- Original Journal: `run-02/state/events.jsonl`; verified accounting:
  `run-02-observer/audit.json`; observer terminal result:
  `run-02-observer/outcome.json`.
- 20 model admissions, all parent; no child/compact/review calls. Known usage
  256,776 tokens; **one failed model call returned no usage**, so this is not an
  exact total token claim. The normal route fallback also consumes the cap.
- Goal stopped at `budget_limit`, CLI exited normally, elapsed 173.34 seconds.
- A/B/C each have an original receipt. No candidate was authored; D/E/F/G/H/I/J
  did not execute. There were no NPU jobs, measurements, diagnostic calls or
  qualifying speedup. The three `write_file` admissions in the raw audit are
  initial readonly document seeds, not model-written candidates.
- Main waste: path/tool discovery. In the first ten completed model calls, eight
  concerned locating inputs/tools and seven included a failed tool. Host-side
  absolute reference paths were visible but could not be read through the
  isolated native tools; driver IDs were mistaken for shell executables.
  Later `pipeline_read` guessed an artifact path and failed. This baseline is
  retained as a harness failure, not evidence of device optimization efficiency.

Run min and the preregistered Cube task under the same mechanism before changing
one general capability/path grounding mechanism. No candidate hint or source
implementation will be added to the improvement.

### Baseline min 01 — completed, unsuccessful

- Fresh session: `.validation/harness-trials/baseline-min-01/run`, same runtime
  mechanism as max (`c5fe484`). Journal and verified observer audit remain under
  that directory and `run-observer/` respectively.
- 20 model admissions; known usage 301,685 tokens; 244.27 seconds; Goal stopped at
  the original limit and CLI exited normally. The model authored a candidate and
  reached D after a rejected freeze using an inaccessible temporary path.
- The final E call failed the fixed-contract preflight: the model added
  `candidate_abi` to the exact contract. Original driver stderr retains
  `Declared semantic, suite, oracle or measurement contract differs from the
  frozen task`. No F/G/H/J or measured gain exists. The outer launch intent is
  conservatively unknown; read-only recovery is checking the original identity
  before another trial is allowed to use the device.
- The 20-call ceiling was not shown in the actual model request. The first,
  eighteenth and twentieth max requests contained neither a remaining-call
  count nor that Goal limit; credits were correctly unlimited. This is a
  separately recorded harness limitation. It is not being changed in the first
  resource-access experiment, to keep attribution clear.

Read-only recovery of min's original E identity confirmed no device dispatch:
`.validation/harness-trials/baseline-min-01/recovery-evidence/d5ee8263bba12032ba12d31094fe28a9fd2bc86a3ce5d310a5b66f4d4e57d773.json`.
The pinned code rejects the extra field before calling trial/transport; no local
attempt, live driver, remote namespace, handle, matching process or device lease
exists. Original unknown intent/receipt bytes were left intact. The separate
Cube baseline was started only after this confirmation.

### Baseline Cube 01 — completed, unsuccessful

- Fresh session: `.validation/harness-trials/baseline-matmul-01/run`; final
  audit: `baseline-matmul-01/final-audit.json`.
- 20 model calls: parent 18 / 315,866 tokens, child 2 / 8,803 tokens; no
  compaction/review calls. Total reported usage 324,669 tokens with no unknown
  usage; elapsed 258.93 seconds. The late delegated review consumed the remaining
  calls reading the candidate and could not complete before the shared ceiling.
- A/B/C exist. The model wrote and edited a candidate; a successful bash edit was
  verified byte-for-byte in the persistent file. Bash write-back itself is not
  the defect. D/E/F/G/H/I/J did not execute. There are no NPU timings or gains.

### Baseline comparison and first hypothesis

All rows are real-model clean trials, **not successful NPU experiments**.
The model changed no oracle, cases, tolerance or measurement settings accepted
by a device job. The only attempted E was rejected before dispatch.

| Task | Model calls (parent/child) | Reported tokens | Model tool calls / failures | Stages | Device jobs / E3 |
| --- | --- | --- | --- | --- | --- |
| max | 20 / 0 | 256,776; one call unknown | 22 / 13 | A B C | 0 / no |
| min | 20 / 0 | 301,685 | 25 / 15 | A B C D; E preflight rejected | 0 / no |
| Cube | 18 / 2 | 324,669 | 26 / 8 | A B C | 0 / no |

The verified machine-readable index is `.validation/harness-baseline-summary.json`.
Model-tool counts exclude the host's three initial readonly document writes.
No trial explicitly called J or read an IR/profiler artifact. There are no
regressing timed cases to enumerate because no paired measurement occurred.

**Hypothesis 1:** model-visible references and capability boundaries disagree.
Map the already approved, pinned oracle/baseline bytes into readonly workspace
references, supply an unambiguous mapping to pipeline binding paths, and state
that driver IDs are pipeline arguments and temporary bash paths are not stable.
This is generic resource grounding: no operator implementation or candidate
source is supplied. Test max/min and the independent Cube task from fresh
sessions. Keep model, 20-call accounting, oracle, measurement, scheduling and
stream limit fixed. Defer remaining-call telemetry and artifact-index changes
so this first comparison changes only one mechanism.

### Resource grounding variant 1 — completed, unsuccessful

Frozen mechanism: `3086d18`. Each task started from new inputs and a new session
under `.validation/harness-trials/grounding-{max,min,matmul}-01/run`. Input
equivalence is recorded in `grounding-input-equivalence.json`; only declared
reference access and capability/path grounding changed. The final hash-verified
index is `.validation/harness-grounding-summary.json`; each trial has a
`final-audit.json`, original Journal, observer transcript and terminal outcome.

| Task | Calls (parent / child) | Reported tokens | Model tools / failures | Observed stages | E3 / speedup |
| --- | --- | --- | --- | --- | --- |
| max | 16 / 4 | 269,245 | 30 / 7 | A | no / unavailable |
| min | 20 / 0 | 325,177; one call unknown | 23 / 7 | A B C | no / unavailable |
| Cube | 20 / 0 | 382,285 | 26 / 12 | A B C D; E launch and recovery | no / unavailable |

All three reached the original shared 20-call ceiling and exited normally;
elapsed times were 113.03, 183.96 and 193.53 seconds respectively. No compaction
or review call occurred. Min call 16 failed with `provider stream exceeded byte
limit` and returned no usage, so its token figure is a known subtotal. No trial
called J, read an IR/profiler artifact, completed F/G/H, or produced a paired
measurement. There are no measured case/family regressions to enumerate.

The reference repair worked: max call 3 and min call 4 read their readonly eager
and benchmark references; the following actual request contained the full
13,744-character benchmark. Host-absolute reference failures ceased. Retain this
as a functional repair, **not a demonstrated optimization-efficiency gain**.
Scatter did not reach device execution. Cube first entered E at call 15 and
attempted recovery at call 16, but SLAI/SOCKS reset before SSH bootstrap. The
original source was `06b28b69c3b647b944a0008fcd77f28c83373af2b2b4cf157994ab627aff73a5`;
the original action was
`operator-cb2a9565-23d5-4f8d-a53c-373ed9b33c73/step/14/model/ds/call/0`.
No handle, compilation result or timing was returned. Keep that effect fenced;
never infer compiler failure or redispatch from a missing response. The first
successful read-only reconnect at 2026-11-11T12:07:57Z found no original remote
attempt directory; its exact receipt remains in the trial's `recovery-evidence/`.

Three independent remaining limitations were observed, and are not combined
into one experiment:

- The six-source ABI document listed field names but omitted generated paths,
  JSON schema, binding symbols and compiler interfaces. Max and min spent calls
  searching unavailable SDK/templates and guessed the interfaces. This applies
  equally to the independent Cube task.
- Successful reference output was replaced by a 209-byte receipt stub after a
  few turns despite ample model context. Max spent calls 6/7/10–15 recovering or
  paging the reference; the receipt's declared total was 29,707 characters with
  source duplicated in content and structured data. The observed pages were
  2,048, 8,192 and 8,192 characters, not one full 29,707-character delivery.
  This is a context-retention issue,
  not simply a model ignoring previously available text.
- The enforced 20-call limit and remaining allowance were absent from actual
  model requests. A trusted telemetry patch is kept as an unapplied private
  draft so it cannot confound the next comparison.

**Hypothesis 2:** a source-versioned, generic compiler-interface document will
reduce avoidable interface discovery and malformed candidates. Supply the same
document to all three tasks, with signatures and placeholders only. Add no
operator implementation, tile size, algorithm or case-specialized advice. Keep
the model, oracle, measurement, scheduling, context policy and call accounting
unchanged. Fresh trials may start only after the original Cube identity is
confirmed inactive and the document integration passes local qualification.

### Compiler interface variant 2 — completed, unsuccessful

Mechanism frozen at `ae36f2f`: one source-versioned compiler interface document,
shared by scatter and Cube, plus task-declared ABI names. Six local tests passed;
no candidate, kernel implementation or operator-specific optimization was
provided. Input equivalence is preserved in
`.validation/harness-trials/native-interface-input-equivalence.json`.

| Task / fresh trial directory under `.validation/harness-trials` | Calls | Reported tokens | Observed stages | Elapsed seconds | E3 / speedup |
| --- | --- | --- | --- | --- | --- |
| max / `native-interface-max-01/run` | 20 | 380,805 | A B C | 133.20 | no / unavailable |
| min / `native-interface-min-01/run` | 20 | 394,606 | A B C; premature J rejected | 153.27 | no / unavailable |
| Cube / `native-interface-matmul-01/run` | 20 | 386,932 | A B C | 163.36 | no / unavailable |

All observers saw the shared model-call limit and normal CLI exit. No trial
reached D/E/F/G/H or produced a device timing. Max's 22 model-tool calls all
succeeded; lack of tool errors did not imply useful optimization progress.
Min made 25 calls: 23 completed, and two failed (an incorrect artifact path and
J before any frozen candidate). Its explicit `npu-ir` call at model call 12
returned `Bind contract and freeze candidate first`, so no IR driver ran and no
diagnostic artifact was read. These are failed clean optimization trials.

For max and min, the full ABI read at model call 3 appeared verbatim in actual
requests 4 and 5. At request 6 it became a 209-character receipt stub. Max spent
requests 9–20 on read-only bash exploration. Min used eleven `read_receipt`
calls paging ABI/benchmark records. The original grounding max wires were only
53–79 KiB despite a configured 2 MiB context-byte allowance. The model's 1M-token
context window, this input-byte allowance, and the provider's 1 MiB response
stream limit are distinct limits.

Retain the interface documentation as an accurate description of the installed
compiler; do not claim improved success or fewer calls. **Hypothesis 3:**
unconditional age-based replacement causes avoidable rereading. The existing
policy stubs successful outputs of at least 8 KiB after two assistant turns even
without byte pressure. Change only retention: measure the owner-composed model
input (system, protected facts, tools and messages), then reduce eligible old
successful outputs only when approaching its authoritative byte limit. Preserve
immutable receipt references, recent exchanges, failed/unknown/protected results,
and the original hard limit. Do not combine Goal-count visibility, stream limits,
diagnostic defaults or receipt pagination changes. Test the same three tasks
with the interface documentation and all evaluation contracts unchanged.

Context-retention qualification caught an initial compatibility error before any
formal trial: native `bashExecution`/extension messages are not plain provider
messages. The corrected implementation uses Pi's pinned `convertToLlm`, maps
receipt and reference-anchor indexes to the converted messages, and verifies
the complete model-visible prefix. It preserves shell, custom, fork and compact
content, including Pi's deliberate exclusion of context-hidden shell messages.

Final local qualification: 15 directed checks (including both original-session
replays) passed; full npm/type/managed checks passed 86 tests with two optional
replay skips, both executed successfully in the directed run. Python discovery
ran 187 tests: 185 passed, one optional skip, one portable-import error; the two
portable replay tests passed after the native conversion fix. These are separate
runs, not a single all-green discovery. Final PTY evidence passed codegen's ten
checks (35 ordinary turns, 48 provider requests), advanced's eleven checks, and
parallel/background's eight checks. Paths are
`.validation/context-retention-codegen-r3`, `context-retention-advanced-r2`, and
`context-retention-parallel-r2`. The initial codegen failure is retained; advanced
r1 was interrupted by the observer and is not classified as a product failure.
The subsequent advanced run naturally completed the large output and compaction.
No real-model context-retention trial had started during this qualification.

### Context retention variant 3 — completed, unsuccessful

Inference mechanism: `1a61ba6`, with the same task, interface document, model
routes, output token cap and measurement contracts. The later `284e5fc` only
corrects read-only audit accounting for usage nested in failure receipts; it
does not change model requests or the Goal ledger. Fresh directories are
`.validation/harness-trials/context-retention-{max,min,matmul}-01/run`.
The verified index is `.validation/context-retention-summary.json`.

| Task | Calls (parent / child) | Known tokens | Calls without usage | Outcome | Device / E3 |
| --- | --- | --- | --- | --- | --- |
| max | 7 / 0 | 106,940 | 0 | Both routes returned malformed responses; awaiting_user | 0 / no |
| min | 20 / 0 | 337,603 | 4 | Shared ceiling; candidate written at call 19, local format check at 20 | 0 / no |
| Cube | 16 / 4 | 336,169 | 1 | Shared ceiling; late child exhausted remaining allowance | 0 / no |

Elapsed times were 42.38, 255.51 and 163.14 seconds. No compaction/review calls
occurred. Max completed A only; min and Cube completed A/B/C. No D/E/F/G/H/J or
paired measurements exist. Missing usage belongs to failed stream-limit calls
and is not zero tokens. Max's two parse failures included 42,197 reported tokens
in original `failure_details.usage`, now included in the audit. The original
SSE responses and exact parser exceptions were not retained, so their precise
malformation cannot be reconstructed. No replacement request was sent by the
observer.

Actual-wire retention is confirmed: max's full ABI and benchmark persisted
through call 7; min's persisted through call 20; Cube's persisted through its
16 parent calls (child inputs are accounted separately). Min read ABI and
benchmark once each, with zero `read_receipt` calls versus eleven in the document
variant. It produced a candidate and the model's own local six-literal/JSON
check passed. This supports keeping retention as a functional repair, but one
trial per variant is not a causal success-rate estimate, and no NPU benefit has
been established. Missing SDK/audit resources still prompted failed searches.
Cube delegated at parent call 16; four actual child model calls consumed the
remaining shared allowance without a candidate or device run.

**Hypothesis 4:** the 1 MiB response-body cap truncates otherwise bounded long
coding streams. Use the already supported provider `max_response_bytes` field
to set 16 MiB for both routes; change no inference code, model, route order,
timeout, `max_tokens=16384`, context limit, call ceiling or evaluation setting.
The finite 16 MiB maximum remains enforced. Four offline checks verified actual
prepared-profile loading into parent/child proposers and failure at the exact
configured boundary. A synthetic fragmented SSE fixture produced 1,736,919 body
bytes for 8,192 text bytes: the same request failed at 1 MiB and completed at
16 MiB. This is transport-mechanism evidence, **not a measured DeepSeek overhead
ratio**. The original stream bodies were not saved. Profile diff and checks are
under `.validation/stream-response-limit-audit/`; fresh input equivalence is
`harness-trials/stream16m-input-equivalence.json`. Provider-error observability,
Goal remainder telemetry and automatic-J defaults remain unapplied drafts.

### Stream-cap variant 4 — max interrupted by a harness protocol defect

`stream16m-max-01/run` admitted 16 parent calls and reported 327,873 tokens
with no missing usage or response-limit error. The model wrote a candidate at
call 12, revised it at 13, checked it locally at 14, froze it at 15 and launched
the original remote build at 16. The build failed because
`scatter_max_custom_tiling.h` was missing. No kernel executed and no timing,
E1, E3 or H evidence exists. Automatic diagnostics attempted collection, but
IR had no compiler recipe and profiling had no runnable kernel; the model did
not invoke J or read diagnostic artifacts.

The remaining four calls were lost to a shared transport defect, not exhausted
by the model. The original handle's exact `started_ns=1794401504504640402`
was rounded by JavaScript to `1794401504504640500`; ReplayContextBoundary
rejected the unsafe number in the tool result. The resulting internal abort
was also incorrectly recorded as a user interrupt. Original receipts confirm
that PID 528130 completed, its result/source hashes agree, the process is gone
and the device lease has no holder. No replacement job was dispatched.
The prepared stream16m min/Cube inputs were left unstarted. This is an
interrupted comparison, not a three-task result.

Functional repair, separate from the optimization hypothesis: project only
job-handle timestamps beyond the JavaScript safe-integer range to canonical
decimal strings before crossing the model/UI boundary. Compare against exact
original receipts through this canonical projection; reject rounded values.
Do not rewrite persisted launches, observations, device reports or result
hashes. Thirty-eight focused tests passed, and read-only recovery of the
original max attempt preserved all 34 artifact hashes and made zero transport
calls. The same helper is used by generic operator pipelines and Cube.

Separately, an internal runtime failure now records `harness_failure` under
the owner lease before effect settlement. Unknown effects remain fenced;
actual Esc/pause, newer Goals and terminal budget state remain authoritative.
The failure cannot masquerade as a user request or auto-resume a poisoned
worker. Ten host-hook tests and nineteen Journal tests passed. Reconnection
alone cannot repair the old rounded native history, so follow-up measurements
use new sessions and candidates with the unchanged 16 MiB profile.

The repaired build passed full npm/type/managed checks (93 passed, two optional
replay skips) and the real Pi parallel/background PTY's eight checks, including
concurrent children, parent input while a background child runs, one completion
notification, and Esc followed by a new working prompt. Evidence is retained in
`.validation/handle-wire-npm.log`, `handle-wire-build.log`, and
`handle-wire-parallel/scenario.json`. Original retention replay checks remain
recorded above. Fresh input equivalence is
`harness-trials/handle-wire-input-equivalence.json`.

### Repaired stream-cap comparison — three clean trials completed

Runtime `c6c56d2`; inputs `handle-wire-{max,min,matmul}-01`. Original
Journal audits, observer outcomes and per-call observations are retained in
each directory. Index: `.validation/handle-wire-summary.json`.

| Task | Parent / child calls | Reported tokens | Seconds | Original remote jobs | E1 / E3 / H |
| --- | --- | --- | --- | --- | --- |
| max | 20 / 0 | 571,475 | 174.39 | 2, both host build failures | no / no / no |
| min | 20 / 0 | 506,216 | 216.82 | 0 | no / no / no |
| Cube | 20 / 0 | 505,512 | 173.90 | 1, host build failure | no / no / no |

All three reached the explicit call ceiling, with no unknown usage, pending
admissions, response-limit failures or unsafe-handle abort. No compaction or
review calls occurred. Total reported usage is 1,583,203 tokens; this is not a
billing estimate. There are no NPU timings or speedup values to report.

Max wrote at call 9, froze at 10, launched at 11, collected at 12 and checked F
at 13. It explicitly used the original missing-header error in C at 14, edited
at 15, froze at 16 and launched a changed source at 17. Its second F at 18
still failed. Call 19 guessed an unavailable standalone build-log path; call
20 read only the first 8,192 of 11,913 receipt characters. There was no third
candidate. Two E launches plus one E collection are two jobs, not three.

Min's J request at call 8 was rejected before a candidate existed; no diagnostic
driver ran. It wrote at 11 and made API/algorithm edits at 13 and 15. Absolute
paths from the canonical workspace failed in isolated bash, whereas later
relative-path commands worked. The call-19 script constructed a proposed change
but wrote back the original bytes; exit zero was not an applied modification.
It never froze or launched a candidate. Cube wrote at 11, edited at 12, removed
a disallowed outer docstring at 15, froze at 17, launched at 18, collected at
19 and received its failed F at 20.

Automatic collection was considered for all three original builds, but IR
reported no compiler recipe and profiling had no runnable kernel. Actual
IR/profiler capture, diagnostic-artifact reads and diagnostic-driven kernel
optimization are all unestablished. Max did use compiler error feedback; that
is distinct from using IR or profiler.

**Next hypothesis:** stabilize the native compiler's common header interface.
Max and the non-scatter Cube task both used a basename tiling include and failed
in host code generation. Max's conditional rewrite passed that stage but then
failed in host target compilation. The adapter currently locates either valid
generated header layout but only supports extra include roots for the kernel
CMake target. A common, recorded include search contract must cover SDK internal
host code generation, host compilation and device compilation without changing
candidate bytes, algorithms, oracle, baseline or measurement. Use a fresh remote
deployment namespace so historical compiler sources remain intact. Remaining-
call telemetry and automatic-diagnostic feedback remain separate unapplied drafts.

### Shared include interface — implementation qualified locally

The compiler now supplies canonical project, `op_host` and `op_kernel` include
roots to every build subprocess, preserving activated SDK/Python paths after
them and omitting empty inherited entries. Device CMake recipes also record
explicit `-I` roots so later IR recipe replay retains them. It changes neither
the six source strings nor the installed SDK. Missing/ambiguous header layouts
and unrelated compiler errors still fail. The public interface is version 2;
its compiler SHA and other provenance records are verified.

Local tests execute actual C++ preprocessing/object/shared-library compilation
through nested SDK-shaped subprocesses for both header layouts and both CMake
interfaces, including relocated kernel sources, inherited Python/SDK headers,
local-header precedence and unchanged parent environment. The old adapter fails
the same missing-header fixtures. An initial portable test compared macOS
`/var` with canonical `/private/var`; all actual compilations succeeded, but four
path-hash assertions failed. Only expected-path canonicalization was corrected.
The full discovery rerun passed 197 tests with one optional upstream dependency
skip (198 total); earlier failure logs remain retained. Evidence:
`.validation/include-roots-python.log`, `include-roots-python-r2.log`,
`include-roots-standalone-after.log`, `include-roots-build.log`.

The prior three remote jobs have been checked read-only: their original PIDs
are gone, original source/result hashes agree, and the old deployment namespace
lock has no holder. This is not a claim of hardware-wide exclusivity. The
original Cube build entrypoint does not clear include variables; downstream
CANN inheritance still requires real qualification.

Fresh inputs `include-roots-{max,min,matmul}-01` preserve all evaluation and
model settings. New remote namespace:
`/home/naie/work/kernelbridge_harness_include_roots_20261111`. The frozen
304-file deployment manifest is `cc39657de0d9bbcfd94e8589b69813ddd1f9c9167670c85719852422168244f2`.
Compared with the original remote deployment, it also carries previously
completed host state/schema and handle-projection repairs; static review of
the 24-file remote benchmark import/template closure finds only the compiler
changed on that entrypoint. Baseline, oracle, measurement and diagnostic code
bytes are identical. Full differences are retained in
`.validation/npu-deployment-include-roots/comparison.json`; this static review
is not a device execution result.

The first upload lost its connection; a subsequent read-only query failed at
the SLAI handshake. No fresh model trial starts before recovering and verifying
that exact deployment identity. No alternate namespace, candidate or device
job has been substituted.

### Operator guidance and usable diagnostic feedback — 2026-11-12

The later user instruction removes model-turn and total-token limits. Counters
remain usage, not implicit budgets. The original uncapped DeepSeek trial remains
unchanged at source `08f1178`; the wrapper's one-hour observer timeout did not
stop its original host. At journal seq 2191 it had admitted 90 calls (71 parent,
19 child), with five original device jobs, zero passing cases and no E1/E3/H.
Three builds reached ACLNN execution failure. Its single explicit IR request
returned the old unsupported `-emit-llvm` error, not usable IR. This snapshot is
not the final result of that still-running trial.

The next harness hypothesis is that missing admitted instructions and unusable
feedback obstruct independent recovery. Clean campaign preparation now freezes
the existing generic operator AGENTS guidance, exact workflow inputs and actual
tool names. The host also delivers those immutable instructions to the child
provider system message. Workspace helpers and private temporary files are
allowed while references, instructions and candidate acceptance remain protected.
F/G/H compare declared source inputs, independently from workspace scratch.

Public-source comparison preceded implementation; see
[the pinned audit](operator-guidance-alignment.md). Codex supplies a complete
open implementation. Claude Code's public repository does not contain its full
CLI core; the checked implementations are sandbox-runtime, official plugins and
the Python SDK. These changes do not claim complete upstream compatibility.

Real online search, official-page fetch and receipt reads passed through the
admitted command tools using explicitly configured Bing RSS, loopback CONNECT
and Google DoH. Earlier certificate, DNS and DDG challenge failures are retained,
not counted as searches. The implementation pins an actual CA bundle and keeps
TLS validation. No model key, conversation or candidate is sent to search.

The original job's full log is now paged and identity-bound: its large trailing
calibration JSON had hidden the earlier concrete error in the old tail-only
response. Diagnostics likewise expose bounded pages with original paths and
hashes. CANN IR extraction preserves the original compilation mode and validates
bitcode, decoded target, compiler and source. An independent compiler-only empty
kernel probe succeeded with the real CANN 9.2 bisheng compiler; this qualifies the
collector, not a candidate or NPU performance. Failed builds and partial profiler
output remain failures. Evidence is under `.validation/ir-qualification/remote-01`.

Initial full JavaScript regression: 164 passed, 3 explicit skips, zero failures.
The first Python discovery used a venv without CPU Torch and reported five
import errors, plus four obsolete task-input/budget assertions. Reusing the
existing CPU dependencies passed all 35 affected dependency tests; updated
contract tests passed 11 with one original-private-receipt skip. All failed logs
remain in `.validation/operator-guidance-tests`. Final full and PTY results will
be recorded after source freeze. New model trials must start without candidates
or inherited sessions. This bundle repairs several independently evidenced
defects; a subsequent speedup cannot be attributed to one change alone.

### Complete uncapped-session replay and repair before landing

The user requested stopping the original trial and auditing the complete
session before further model testing. The original host recorded `paused`
after 91 calls: 72 parent and 19 child, six native jobs, zero completed candidate
cases and no E1/E3/H. The control acknowledgement timed out after 30 seconds,
but the single original pause committed after 3.516 seconds; its following
prepare-turn returned paused without a new model admission. No repeated control
or replacement job was issued. The old trial remains paused.

The replay covers every provider request and response, all associated tool
outcomes, 13 exact candidate versions, and six original device receipts. Its
full report and frozen original Journal are retained in
`.validation/session-audit-20261112/trajectory-full.md` and
`original-journal-at-pause.jsonl.gz`. Important distinctions:

| Observed issue | Evidence and repair |
| --- | --- |
| Child pages skipped unseen text | Both children received only about 3.8 KiB of an 8 KiB ABI page, while the next cursor remained 8192. Their gap covered 4364 bytes of interface guidance. Projection now fits the complete observation envelope and advances only past visible content; code-point, UTF-8 byte and base64 cursors remain distinct. Original receipts are unchanged. |
| Successful build concealed an SDK error | Jobs 2–4 compiled with exit code zero while independent build logs reported unregistered tiling data. Those logs were absent from every model request. Build-step receipts now retain source identity, actual return codes and readable logs, including successful steps. Diagnostic text does not override the true exit code or correctness result. |
| Runtime root cause was lost | The original first lines reported GetWorkspaceSize / host tiling failure; the old 12 KB tail contained later calibration JSON. Full log recovery and SDK severity recognition expose the original line and exact read path. These jobs had not entered candidate device execution. |
| IR collection used an unsupported mode | The original CCE recipe rejected the old appended `-S -emit-llvm`. The mode-preserving collector was independently qualified with the real compiler. That fixture does not retroactively qualify the old failed diagnostics. |
| Original job root has a legitimate alias | The declared remote work root resolves to a different canonical prefix. Artifact reads now resolve that bound root while still checking original handle, candidate, result and file hashes, and rejecting internal symlinks or escaped artifact paths. |
| Pause reply was expensive | The same retained Journal required five old status transactions, versus one in the already merged main implementation. Offline warm status was about 4.8 seconds versus 0.14–0.16 seconds; no runtime timeout was weakened. |

General failure status was present in the next parent request after every
evaluation; this is not evidence that the SDK root cause was delivered. Exact
search of all 91 requests found neither the registration diagnostic nor the
host tiling error. The replay also found unsupported model claims about output
coverage, kernel naming and exhausted budgets. These remain model decisions,
not measured kernel behavior or reasons to invent a failure attribution ratio.

Generic CANN API notes now identify namespace, generated accessor and typed
tiling-storage boundaries from the actual provisioned SDK headers. They contain
no operator algorithm or candidate. New trials and deployment snapshots prepared
before this audit are explicitly not launch-ready. The user's latest priority
is to validate and push the repaired harness to main before further performance
experiments; no eager speedup is claimed by this delivery.

Final local qualification after merging `0b14f8a`: Python 400/400 passed with
the CPU dependencies and original replay fixtures; the full TUI suite passed
27/27 without flaky or environmental outcomes. Its full JavaScript run passed
165 with three explicit condition skips, zero failures. The original NPU trial
remains paused; this landing does not establish candidate correctness, ABBA
acceleration or promotion. The audit and validation artifacts are archived at
`.validation/operator-guidance-delivery-20261112/` after delivery, with their
original contents and historical path identities retained.

## 2026-11-12: clean instruction-delivery trial and pre-dispatch rejection

After `ad03725` was pushed to main, a separate clean max session was started
from that exact source. Its new deployment contained 306 verified files, used
an idle device 0 through the existing Hong Kong transport, and retained the
original eager, cases and measurement protocol. No candidate or conversation
was copied into the new workspace. Total model-turn, token and credit limits
were absent, following the user's later instruction; the earlier 20-turn
target is not a limit for this run.

The session `application-8e462fb0-021b-478c-986e-8d7b655a3ee6` is retained under
`harness-eager-validation/KernelBridge/.validation/harness-trials/max/`.
Its first 21 completed model calls were parent calls, using 818,827 reported
tokens. The first candidate was written at call 8; the first composite
evaluation was requested at call 12. There was no original device attempt or
handle, build, correctness result, IR/profiler artifact, pair or promotion.

All 21 actual provider request bodies contained the complete admitted
`AGENTS.md` (14,732 UTF-8 bytes) and `AGENTS.task.md` (2,092 bytes). All 12
successful artifact-read pages were present in the subsequent request. This
establishes instruction and page delivery for this parent path; it does not
establish that every agent mode or operating system was exercised, or that the
model used every instruction correctly.

Two independent harness failures explain the stalled run:

- The candidate contained all six literal source assignments, preceded by a
  Python module docstring. The final source parser rejected that first AST
  node before creating an attempt or invoking the transport. The pipeline had
  already written its dispatch marker, so it incorrectly exposed this local
  rejection as an unknown launch. Subsequent recovery looked for an attempt
  that did not exist. The outer tool process succeeded in returning this
  business failure; its exit code was not the launch subprocess's exit code.
- The host durably queued a continuation after call 21, but its input repeated
  the full task, including instruction and ABI seed files. That exceeded the
  normal 64 KiB input limit. The UI caught the validation error while the Goal
  remained active. This was an input-delivery failure, not exhausted tokens or
  a measured device stall.

The model had read the parser's source earlier, but did not read the actual
launch traceback. It read the later collect traceback instead. The complete
request/response audit, source changes, original receipts and verified Journal
snapshot are in the same worktree's
`.validation/trajectory-observation/max/`, including
`stage-21-causal-audit.md`. The original candidate and session are retained;
repairs are developed separately and subsequent performance trials start
clean. No eager speedup is claimed from these 21 calls.

The original session was then paused with one control request. Journal sequence
623 atomically records the paused Goal and interruption of the old queued
policy; the 21-call audit through sequence 621 is preserved separately. No
candidate or provider prompt was changed to rescue that session.

### Repairs derived from the retained 21-call trace

Three independently replayed repairs are under local qualification. Instruction
delivery now uses one admitted immutable snapshot for the parent, foreground and
background children, and the legacy model adapter. Project mode admits the
selected workspace's `AGENTS.md` and `AGENTS.task.md`; isolated mode uses the
task's readonly seed files. Empty snapshots do not fall back to ambient files.
This does not add ancestor discovery or claim qualification on another OS.

Native source validation accepts an optional module docstring followed by the
same six unique literal assignments. It remains a pure AST check: imports,
execution and additional statements are rejected. New actions return a
source-bound local rejection before dispatch. An existing action is recovered
first using its original identity and bytes, even if today's parser would
reject its historical format. Contract or source conflicts still fail closed;
unknown effects never become a new launch or a `not_started` receipt. The same
parser serves scatter and Cube, without changing either implementation,
oracle, eager baseline or measurement protocol.

Continuation inputs now reference the original task identity and evidence
instead of repeating seed-file bodies. Replaying the original queued policy
reduced its size from 66,794 to 1,898 UTF-8 bytes while retaining the complete
objective and decision. The normal 64 KiB user-input limit is unchanged.
Admission precedes queueing; definite rejection is durable and visible, while
an uncertain acknowledgement is reconciled using the original input ID.
Concurrent Goal changes remain protected by the Goal identity check. These
instruction and input repairs apply to ordinary coding and non-scatter tasks.

The first full local pass is retained, including failures: Python discovery
ran 421 tests with one failure, ten errors and one optional dependency skip;
TUI acceptance passed 26 of 27 groups. Seven Python errors came from test-only
hardcoded `/usr/bin/node`; two delegation errors did not recur in the explicit
Node-environment rerun, whose 13 cases all passed. A real cached-action recovery
regression and a wall-clock-dependent pending-job fixture were repaired rather
than hidden. The TUI runtime group's three worker deadline errors occurred
under heavy load; the same cases passed in the Python run. That failure remains
recorded and requires a successful follow-up. JavaScript passed 190 of 193 tests,
with three explicit condition skips and no failures. Logs are retained in
`agent-guidance-parity/KernelBridge/.validation/acceptance/`. These are harness
checks, not evidence of NPU correctness or eager acceleration.

After those fixes, full Python discovery passed 424/424 tests, with no skips,
using explicit Node resolution, the existing CPU dependencies and original
private replay fixtures (`.validation/acceptance/python-r2.log`). No production
deadline was increased. The auxiliary task remains the independent Cube task
specified in [the task report](harness-auxiliary-task.md); its matrix execution,
tiling, accumulation and workspace requirements differ from scatter. It is
still a fixed-shape contract, not evidence of generalization across matrix sizes.

The isolated TUI runtime follow-up also passed all seven tests; its runner
passed deployment and runtime (2/2) in
`.validation/acceptance/tui-runtime-r2/acceptance.json`. Combined with the 26
successful groups from the retained full run, every required local TUI group
has a passing result. This is a full run plus a focused follow-up, not a new
single 27/27 run. There were no further production changes after qualification.

## 2026-11-12: clean `d07af47` max/min results and terminal recovery

The next clean batch used frozen source `d07af4769381b34a456fa4e2aa343fff4b1c4833`
in `agent-guidance-parity/KernelBridge/.validation/next-clean-trials/batch-d07af47-01/`.
The workspaces started without inherited candidates or conversations. The
original suite, oracle, same-device eager comparator and measurement protocol
remained fixed. These sessions followed the later uncapped Goal instruction;
their observed call counts are not 20-call completions. Calls, native attempts
and accepted results remain separate accounting units.

| Clean task | Settled model calls | Reported tokens | Original native attempts | Observed qualification |
| --- | ---: | ---: | ---: | --- |
| Scatter max | 41 parent; no child/compact/review calls | 2,920,583 | 4 | 6/6 correctness, same-device eager geometric speedup 5.250296×, E1/E3 and H |
| Scatter min | 66 parent; no child/compact/review calls | 6,458,433 | 3 | Two compile failures; third original job unknown; paused with no E1/E3/H |
| Cube auxiliary | 0 | 0 | 0 | Not started in this batch |

Max first reached complete native correctness and positive timing at call 33,
attempt 4. The later F projection at call 39 reused that same original attempt;
it was not a fifth launch. G followed at call 40 and H at call 41. Host E1/E3
were recorded at Journal sequences 1056/1057, with the terminal boundary at
1069. Every case improved; the lowest case was 2.652006×. Independent offline
recomputation verified all 25 committed G artifacts and reproduced the original
qualification: log paired delta 1.5881532232479976 exceeded the measured
independent eager/eager threshold 0.02801543203215286. This retains the original
three ABBA blocks and pooled-arm median statistic. It is one accepted paired
result, not independent stable reproduction, a holdout, or E4/platform acceptance.

The max diagnostic path also gives a narrower result than “the model used IR”.
Attempt 3 failed correctness after a successful build, and automatic IR capture
completed. There were zero explicit J requests and zero reads of actual
IR/profiler artifact contents. The winner had no profiler evidence. The model
changed its implementation after reading runtime error pages; its structural
explanation was a hypothesis, not an IR-confirmed mechanism. Both frozen
instruction files were fully present in all 41 provider requests. No subagent,
fork or compact execution occurred in this trajectory, so it does not qualify
those contexts empirically.

Min's compiler diagnostics reached the following actual provider requests, and
subsequent source revisions responded to specific host/device compile
errors. Attempt 3 was acknowledged at call 30 but never supplied a final
benchmark receipt. Ten explicit collect requests and two internal collect
stages still produced no terminal result. The original unknown launch fence
rejected the attempted replacement at call 49. The first no-tool blocked reply
at call 52 was followed by automatic Goal continuations; the original session
was explicitly paused after call 66, atomically at sequence 1632, with no pending
model/executor action at checkpoint 1633.

The retained min observation had `alive=false` and no result. A later read-only
device check, recorded separately in `.validation/continuation-state.json`,
found the original process absent and build success plus 72 eager calibration
samples. Neither observation supplied an actual exit code/signal or complete
candidate result. The original attempt therefore remains unknown, not an
inferred compile failure or measured performance result. No replacement job or
Cube job was launched to bypass it. Model text later mixed attempt 2's PID/path
with attempt 3's source identity; the original launch receipt, not that text,
defines the recovery identity.

The final reports preserve per-call wire references, source identities and
Journal hashes under
`agent-guidance-parity/KernelBridge/.validation/next-clean-trials/observations/`:
`max-fea2ecdb6001d90b/final-report.{json,md}` and
`min-919b13c288fca341/final-report.{json,md}`. Original Journals and device
artifacts remain in place. Read-only qualification replay is retained in the
new recovery checkout's `.validation/qualification-recovery/replay-report.json`.

### Separate recovery changes derived from these observations

The new `harness-terminal-recovery` checkout starts from `d07af47` and changes
the harness, not the frozen trials, measurement contract or operator answers.
The proposed efficiency improvement is fewer tool-interface failures and fewer
model continuations without new evidence. Several independently demonstrated
bugs are repaired together; a later efficiency difference cannot be attributed
to one variable without a separate controlled comparison.

- Ordinary Bash no longer silently adds `errexit` and `pipefail`. The tool
  describes its normal final-exit-status behavior, clean environment, isolated
  filesystem and success-only authorized writeback. Explicit strict options
  remain available. The max audit found six executed commands returning exit 1,
  not six admission denials; some were out-of-workspace probes or a model AST
  bug. This repair does not grant host-repository access or establish six saved
  turns. Predictable shell semantics apply to ordinary coding and every operator.
- Qualification replay reads the original retained noise-file bytes as well
  as checking the canonical payload. Re-serializing an embedded dictionary
  after canonical key sorting had incorrectly changed the reconstructed file
  hash. The original file SHA and payload SHA are both still required; changed
  bytes, payloads, missing arms and duplicate fields fail closed. The measured
  threshold, case coverage and timing are unchanged. This supports any declared
  calibration protocol, including the separately frozen Cube protocol.
- Unknown native observations expose a verified original handle separately from
  settled execution handles. Action, source, immutable launch binding and exact
  original ACK must agree. Lost or null ACKs require the recovered original
  launch/request bytes and pinned validator; a later PID or boolean claim is
  insufficient. Foreign terminal handles are rejected before they can clear
  the original launch fence. These rules also cover non-scatter asynchronous jobs.
- A supervisor retains actual child exit evidence bound to process birth,
  source, original action and supervisor identity. A verified exit without a
  qualifying benchmark yields an explicit nonqualifying failure, not fabricated
  cases or timing. Missing exit evidence stays unknown. Benchmark output written
  before supervised exit is only a provisional observation; its remote-reported
  byte hash is not presented as independently verified local bytes. Legitimate
  deployment-root aliases remain distinct from forbidden internal symlinks.
- Continuation metadata describes semantic progress under the stable original
  job/artifact identity. New wrapper IDs and timestamps do not count. Waiting
  for an unavailable original receipt blocks the next automatic policy without
  settling the effect; unrelated reads cannot clear that wait. Verified live
  work and locally executable next phases remain resumable. In particular, a
  bounded wait ending before first launch or between terminal E and ready F
  is not classified as an external-receipt wait. These distinctions apply to
  ordinary background tools as well as both scatter modes and Cube.

The offline min counterfactual at
`.validation/continuation-replay/min-call52.json` applies new metadata to the
verified historical input boundary at sequence 1307. The new consumer returns
`awaiting_original_receipt` there, before admitting another automatic policy.
This is a producer/consumer replay, not a new 52-call model run, a measured
14-call saving, or evidence that min would have completed. No later observation
was backdated and no original input, candidate, receipt or Journal was changed.

Focused local regression covers original-handle fences, forged collectors,
lost/null ACKs, provisional results, actual failure projection, ready-F deadline
continuation, byte-preserving qualification and prepared-tool imports in fresh
processes with an empty `PYTHONPATH`. The final eight-module run executed 110
tests: 109 passed and one optional private historical-receipt fixture was absent.
Its log is `.validation/terminal-recovery/pipeline-review-final.log`. Full
Python/JavaScript/runtime and PTY acceptance of this recovery checkout remains
pending at this entry. Fresh device
trials and independent stable repetition also remain outstanding; none of the
offline fixes or focused tests completes the remaining min/Cube evidence.

### Acceptance follow-up for terminal recovery

Full Python discovery passed 470/470 with no skips
(`.validation/acceptance/python-r2.log`). Its retained first run had one
legacy-ACK compatibility failure among 469 tests; the driver protocol was
corrected without changing the old assertion. The complete TUI second run
passed 27/27 at timeout scale 1.0, including 121 authority tests and full npm
tests (212 passed, one filesystem-dependent skip). Both original JavaScript
session replays passed with their original fixtures. The first TUI run's two
failures are retained: an overstrong narrow-footer assertion and a tool-free
Goal reason compatibility regression. The production layout and advanced
assertion were preserved; all required groups passed in the subsequent full
invocation, not just a collection of focused reruns.

The real Linux CPU supervisor probe passed all six checks, including a killed
supervisor whose living child retained the fixture lease and whose result
remained unknown. The probe used an independent temporary namespace, not the
original min deployment or the device execution lock. Exact hashes and raw
output are in `.validation/supervisor-local/linux-collection.json`. This is
lifecycle qualification, not a new NPU optimization trial or performance claim.
Full details and retained failure logs are indexed in
`validation/tui/ACCEPTANCE.md`. Min, Cube and stable fresh repetition remain
incomplete; no unknown original job was settled or replaced during these fixes.

### Main rebase and persistence-window qualification

The recovery branch was rebased onto main `c8a2e45`. Full Python discovery
passed 470/470 with no skips in
`.validation/rebase-main/python-full.log`. The subsequent full TUI run passed
26/27 groups, with only the JavaScript group failing: an exact 65-second Goal
interval displayed as `1m04s` after epoch-nanosecond conversion lost precision.
The failure remains recorded in `.validation/rebase-main/tui-full/`; it is not
an environmental timeout or a passing full run. The repair keeps subtraction
and division in `BigInt`, preserves the existing live-clock assertions and
passes all six plan-presentation tests. This changes display arithmetic, not
host usage accounting, budgets or measurement timing.

Independent recovery review identified a second distinction between waiting
for external evidence and executing an available observation. The original
child may exit just before its still-live supervisor persists the actual wait
receipt. The new `settling` state is permitted only after original
launch/request bytes, pinned code and supervisor birth are verified. The
pipeline remains unknown and fenced while collecting that original receipt;
it does not insert an observed handle into settled executions. A repeated
unchanged observation retains the same semantic fingerprint. Missing proof,
supervisor loss and legacy liveness claims do not grant continued execution.
The actual original exit, once present and verified, settles only that identity.
This is a generic asynchronous-process boundary, not an operator-specific hint.

Native local tests passed 45/45, pipeline/consumer boundary tests passed 37/37,
and an independent Linux CPU lifecycle probe passed 7/7. The Linux probe used
its own namespace and lease. Its first six checks used unchanged production
code; the seventh used a separate instrumented test copy to hold the real
child-wait/persist boundary. No production delay, NPU job or original-trial
mutation was involved. Source hashes and original logs are retained under
`.validation/supervisor-settling/` and `.validation/terminal-recovery/`.

The final focused TUI validation passed 7/7 at timeout scale 1.0, with no
failed, flaky or environmental group. It covers deployment, full JavaScript,
authority, interaction, advanced, Goal review and Goal plan. Full npm passed
225 tests with one case-sensitive-filesystem skip and no failure; authority
passed 162/162 and advanced passed all 11 checks. All 30 referenced evidence
files matched their recorded lengths and hashes in
`.validation/rebase-main/tui-final-focused/acceptance.json` (SHA256
`fa1b001605f3dd92423f76743d52c951dc261eb53eab525da70dfd292fb754ae`).
The qualification is the earlier full run's 26 passing groups plus this focused
follow-up, not another full 27/27 invocation. The failed full-run log and all
earlier failures remain retained.

There was no new optimization trial during these repairs. Max still represents
one accepted six-case 5.250296× paired result, not independent stable
reproduction or E4. Min remains paused with its third original attempt unknown
and no E1/E3/H; Cube has not started in that batch. The prior call-52 replay is
still an offline counterfactual, not a measured reduction in model calls. These
separate fixes must not be presented as evidence for a single causal explanation
of a later optimization result.

The last original-min identity recheck is retained in
`.validation/original-min-final-recheck/recheck-report.md`. It found the original
PID absent, no matching lease lock, no final benchmark or exit bytes, and
unchanged original source/intent/launch/intermediate-status evidence. Those
facts still mean unknown, not a reconstructed failure verdict. A new supervisor
cannot supply an old process's missing exit retroactively. The next clean batch
is planned independently, preserving the old unresolved session and artifacts;
it is not a retry of that original handle. No new clean batch is reported as
started by this entry.

### Clean e19 min: native compile failure and lost diagnostic binding

The terminal-recovery changes were merged and pushed to main as `e19c804`.
A subsequent independent clean min session is recorded in
`.validation/next-clean-preparation/trajectory/observations/min-7eec272bd535fc56/final-report.md`
and its JSON/index files in the `harness-terminal-recovery` checkout. This
session reached 37 settled model calls: 30 parent and seven child, with no
compact/review calls and 1,687,106 reported tokens (1,564,147 parent and 122,959
child). The first candidate appeared at global call 23 and the first evaluate
at call 24. There was one actual native attempt and no F/G/H, E1 or E3 result.
No explicit J invocation or actual profiler/IR-content read occurred. This run
did not meet the 20-call qualification objective.

The new min failure is distinct from the older unresolved d07 min attempt.
The new original benchmark report records a compile failure with
`build_completed=false`; its first diagnostic is
`OPS_CHECK_NULL_WITH_CONTEXT was not declared in this scope`. The original
supervisor separately records return code 1 with no signal. Both objects are
bound to the original source and handle. The benchmark SHA256 is
`a13c9ef72ea37be8dcddb3c5e4434a0dbe2c0e72b45162161fc0d7cda4fdc08a`;
the distinct supervisor-exit SHA256 is
`450e5b3934e9904f37c1408ee8ecc3d3dff8f5f7116d0979ffa4464c42d01ef3`.
This is a known native failure, not a missing remote exit.

The local projection instead selected `result_origin=supervisor_exit`, retained
`remote_result_sha256=null`, and lost the benchmark digest used by the already
captured 14,758-byte original run-log receipt. The adapter's strict log verifier
then raised `Original run log binding changed`. The outer evaluate returned
pending with `original_handle:null`. The failed E artifact manifest omitted the
captured run/build log. Calls 25–37 received the generic failure, exit receipt
and binding error but not the specific compiler diagnostic. The model resumed
the original evaluation at call 25 and attempted original collection at call
29; neither operation launched a second native job. No candidate edit followed
call 23. The record does not support saying the model ignored an advertised
compiler-error artifact.

Two separate guidance problems also occurred. Preparation hardcoded
`max_trials=1`, which the parent interpreted as one allowed native candidate
attempt, despite the task appendix distinguishing a Goal Trial/session from a
candidate or device attempt. Aggregate model-turn, token and credit caps were
unset. At global call 37 the model explicitly requested `update_goal(blocked)`;
the host respected that request and entered `awaiting_user` (Goal Journal
sequence 1051, settled boundary 1063). This was not the host's automatic
`awaiting_original_receipt` boundary. Separately, the read-only child's prompt
instructed it to use `finish`, while its actual registry did not include that
tool. Global call 11 was rejected as `unregistered_tool`; call 12 supplied a
normal-text fallback. The child eventually delivered its 2,349-byte advisory
summary. All 37 requests, including the seven child requests, contained the
frozen project/task guidance.

The `native-failure-binding` work addresses these as three independent generic
repairs: preserve the independently bound benchmark and supervisor evidence
through terminal failure/log projection; keep an unspecified campaign budget
absent while respecting explicit settings; and generate child completion
instructions from the actual registered tools. Original byte hashes, source,
handle and log identity checks must remain strict. None of these changes
supplies an operator implementation, alters the frozen workload/oracle/timing,
or qualifies a failed attempt. A later reduction in calls cannot be attributed
to any one of these changes without a separate controlled comparison.

The first full Python acceptance invocation ran 487 tests and failed with three
failures and two errors; one additional test was skipped. The five unsuccessful
checks exposed a compatibility regression: removing the old nullable
`application.max_model_turns` field changed the manifest schema, although no
numeric budget was intended. The nullable field was restored, preserving an
unset numerical limit. The skipped check lacked this checkout's link to the
existing shared scatter dependencies; after restoring that link, the DQ check
passed separately (1/1). The old max/Cube historical-receipt optional fixture
also passed separately (1/1), recorded in
`.validation/failed-benchmark/historical-receipts.log`. The failed full run is
retained in `.validation/acceptance/python-full.log`.

The first full TUI invocation passed 26/27 groups at timeout scale 1.02, with
one failure and no flaky or environmental outcomes. The interaction assertion
required the full directory basename even when a long branch name correctly
shortened that footer. Its test now requires the precise allowed shortened
prefix; production UI behavior was unchanged. All 144 referenced evidence files
matched their recorded hashes. The original failed full-run receipt remains
`.validation/acceptance/tui-full/acceptance.json`, SHA256
`aa74b0380c50d55eb2cb4e6cac6f2787ca2e625364da8769669519a4a6d807c0`.
`get_goal` also now reports `policy.max_steps` in its separate scope rather than
presenting it as a numeric aggregate Goal budget.

The rebuilt second full Python invocation passed **487/487**, with no skips,
in 229.800 seconds (`.validation/acceptance/python-r2.log`). The final focused
TUI invocation passed **7/7** at timeout scale 1.0, with no failed, flaky or
environmental group. It includes full npm (**225 passed**, one actual
case-sensitive-filesystem skip) and authority (**167/167**). All 30 referenced
evidence files matched their recorded lengths and hashes. The receipt is
`.validation/acceptance/tui-final-focused/acceptance.json`, SHA256
`d2343ecb80f4ce860c859e7d4927ef2b23db63a3eaa91ee3894e15fb31ea64a7`.
This TUI qualification combines the first full invocation's 26 passing groups
with the seven-group follow-up; it is not another full 27/27 invocation. The
first Python compatibility failures, missing-dependency skip and first TUI
assertion failure remain retained. These results qualify the repaired harness;
there is no successful optimization result for this new version yet. A new
independent clean trial is still required.

### Best max session preserved independently

The complete best observed max session is retained outside the disposable
worktrees at
`~/.kernelbridge/optimization-archives/scatter-max/d07af47-application-f44e42d3-bf1f-4add-963a-2719fda4202f`.
It contains 589 byte-preserved original files, all four native attempts and their
failures, the complete original conversation/Journal, and independent review
records. The final archive manifest indexes 601 files including archive metadata
and review artifacts; verification passed for all indexed files. The supporting
record is `.validation/reward-integrity/max-archive-verification.json` in the
`harness-terminal-recovery` checkout. The archive preserves original absolute
locators and also provides relative indices; it supports reading/restoring the
conversation, not a claim that automatic scatter execution can be resumed after
relocation. Original live-session directories were not moved or deleted.

The independent source and receipt audit identified no reward-hacking behavior
in the complete reviewed candidate, and reproduced F/G/H consistency and the
original timing/qualification calculation without running a new device job.
Its six fixed cases passed and the same-device `torch_npu` eager composition
comparison has geometric mean speedup 5.250296121197787×, log gain
1.5881532232479976 above the original calibrated threshold
0.02801543203215286. This is not a comparison against the `torch_scatter`
extension. All 41 parent calls, 2,920,583 reported tokens and four native attempts
remain part of the session cost; the archive does not retain only the winner.

This remains one qualified six-case result, not independent stable reproduction
or E4. The successful candidate has no profiler trace or IR artifact. The review
did not independently reread remote binaries, obtain the historically unavailable
full run log, or attest a native extension outside its evaluator process. It
makes no claim for unseen shapes or an official score. The old d07 min attempt
remains unknown; the new e19 min attempt has a known compile failure whose
feedback was projected incorrectly. Neither has min qualification, and Cube has
not started in these recorded batches. No fresh successful optimization result
is implied by the repair or archive review.

### Sampled device isolation and explicit remeasurement

The native workflow previously bound a result to its source, process and exit,
but did not independently establish whether the declared target device had
foreign occupants or whether process observations covered the measurement
lifecycle. A same-device eager comparison alone does not establish that property.
The new contract makes the isolation policy and its hash explicit for newly
prepared profiles. It preserves existing prepared contracts and historical
receipts; missing historical evidence is not retroactively upgraded.

The evidence claim is deliberately `sampled_observed_clear`, scoped to
`cooperative_same_uid_namespace`. The observer records complete raw device
process tables, the declared target and physical mapping, process birth and
namespace identities, and the process-view proof needed to interpret reported
PIDs. It verifies which observed processes belong to the original child or its
descendants. Unknown identity or mapping, incomplete observations, temporal gaps,
and foreign occupancy cannot become a clear verdict. A shared physical-device
lease coordinates cooperating processes in its declared same-UID namespace;
it is not proof of absolute device exclusivity. A guard also checks visibility
before launch and after SDK activation, so activation cannot silently remap the
logical target after admission. This is provenance and execution isolation,
not a change to operator semantics.

A remote supervisor may refuse device admission before spawning its child.
This is an independent `kernelbridge.native-admission/v1` result, not a local
source preflight error or a compiler failure. The core retains the original
`dispatch-boundary.json`, then independently verifies the three original byte
artifacts (`admission.json`, `isolation-samples.jsonl`,
`execution-request.json`) against the original action, source, device, policy,
request and four code pins, including the SDK entry boundary. Only a verified
`not_started` / `dispatched=false` receipt can settle that launch fence as a
launch rejection. A lost ACK may be recovered through the original action and
original artifacts; neither an adapter flag nor the absence of a PID is enough.
Missing, mismatched or uncommitted evidence leaves the original launch fenced.
Resource-busy and observer-unknown reasons remain visible in the feedback.
Status and artifact reads never dispatch work.

A true original terminal exit with nonqualifying isolation is a different case
from an unknown execution. The core independently replays the original four
artifacts and immutable source/handle binding before permitting an explicit new
measurement of those same source bytes. The prior handles, samples, failures
and receipts remain retained. Each new E gets its own F bound to that actual
handle; cached F/G from the previous execution cannot supply the new result.
Valid clear executions still reuse their existing receipts. Unknown executions
cannot be replaced. Admission refusal and nonqualifying terminal isolation
produce an external-wait boundary rather than an automatic busy retry loop;
resource resolution or explicit user continuation is required. No selection by
speedup or deletion of unfavorable samples authorizes this retry exception.

For required isolation, G, H and host acceptance independently verify the raw
`isolation-samples.jsonl`, `isolation-receipt.json`, `execution-request.json` and
`execution-exit.json` bytes, policy and pins, original source/action/handle,
result digest and actual exit. All four ABBA projections must refer to the same
original evidence. A model-written clear flag or missing original artifact
cannot establish E3. If there is no performance baseline, a complete F can
still establish the first correctness incumbent; that bootstrap establishes
neither speedup nor E3. Reintroducing a baseline requires its complete paired
and noise-qualified evidence.

These changes do not rewrite the eager baseline or the measurement-function
AST, case suite, oracle, ABBA aggregation, timing values or calibrated noise
threshold. The extra policy field records an evidence requirement. Existing
budget semantics are unchanged: unspecified ceilings remain absent, and
explicit configured limits keep their own scope. No operator answer or
candidate implementation is added. A future change in optimization efficiency
cannot be attributed to this feature alone; this round includes independent
admission, process observation, recovery and qualification changes.

Focused local validation used actual CPU subprocesses plus synthetic raw device
observations consumed by the real pure validators. The final native
supervisor/entry/isolation group passed 60/60
(`.validation/device-isolation/supervisor-entry-r3.log`); this includes the
21-test raw-isolation group. The adapter/recovery group passed 65/65
(`.validation/device-isolation/adapter-tests-04.log`). Core admission and
remeasurement passed 11/11 (`.validation/native-admission/core-r2.log`).
Qualification/H/host acceptance and existing noise qualification passed 25/25
(`.validation/pipeline-isolation-r2.log`). These groups overlap and must not be
summed into an invented unique-test total.

Portable process/lease tests on Darwin passed 31 tests with one genuine
Linux-only fdinfo check skipped (32 total). A separately dispatched Linux CPU
qualification passed 31 tests with one Darwin-only check skipped (32 total),
with exit zero and original source hashes unchanged. Its receipt is
`.validation/device-isolation/linux-helper/cpu-helper-c113a24370124307a5bab935013a3a2b/qualification.json`.
That run used only standard-library process creation, `/proc`, `flock`,
`pass_fds` and fdinfo in its own namespace. It qualifies real Linux process and
inherited-lease behavior, not NPU occupancy, a physical process-view mapping,
a kernel, a compiler or optimization performance. The controlled target
process-view/device probe remains a separate evidence requirement.

Earlier failures remain retained. Initial preparation rejected the fourth
entry-code pin because core metadata still allowed only three; the schema was
updated. A synthetic no-start fixture initially omitted the producer identity;
the fixture was corrected to the real protocol. Two guidance assertions needed
to account for the new policy field while preserving all original measurement
values. Handle-selection regressions changed the existing rounded-handle error
and excluded null-ACK recovery before a terminal result; the original contracts
were restored. The six-module core run retained its 92-test result: 90 passed,
one null-ACK compatibility error, and one optional historical-receipt fixture
skip. The later 65-test adapter run includes the complete pending-observation
module and passes that recovery case. The previous failed invocations are not
rewritten as passes.

The first full Python invocation ran 585 tests in 209.115 seconds: 583 passed,
one failed and one Linux-only fdinfo check was skipped on Darwin. The failed
original e19 compile-receipt replay returned unknown because its historical
supervisor pin did not match the new verifier's expected pin. Its unchanged
assertion and complete failure remain in
`.validation/device-isolation/python-full-r1.log`. The subsequent complete
Python invocation passed 585 tests with one genuine Linux-only skip (586 total)
in 248.895 seconds, exit zero
(`.validation/device-isolation/python-full-r2.log`). The real Linux CPU run's
31 passed/one Darwin-only skip supplies the complementary platform qualification;
the Darwin skip is not presented as an executed Linux check.

The first full TUI invocation passed 15 groups and failed to run 12 groups
because the root invocation omitted the existing `ui-test-deps` directory from
`PYTHONPATH`, leaving `pyte` unavailable. That setup failure is retained under
`.validation/device-isolation/tui-full-r1/`. The dependency path was supplied
without a product change. The second full TUI invocation passed **27/27**, with
zero failed, flaky or environmental groups at timeout scale **1.0**, and returned
zero. All **145** referenced evidence files independently matched their recorded
lengths and SHA256 digests. Its acceptance receipt is
`.validation/device-isolation/tui-full-r2/acceptance.json`, SHA256
`86dd5ce66ccdb1e03511b9041d7452dc52d24b4d259ac983c928e60c0cf743ec`.
This is an actual complete passing invocation, with both earlier failed runs
retained.

These full results qualify the main isolation implementation at that boundary.
A subsequent usability follow-up has added the public
`examples/scatter/prepare_npu_isolation.py` entry and its pinned receiver. The
CLI exposes `initialize`, `prepare`, `dispatch` and `recover`. Inputs explicitly
select the SDK/Python profile, authorized transport and any pinned transport
dependencies, an existing remote root, the logical device and a fresh local
output directory; no deployment host, device or operator is hardcoded. The
prepare-only path never invokes transport. Dispatch stages a bounded context
probe in a separate infrastructure namespace. Only a valid original terminal
proof produces `initialized-profile.json`; an unknown result retains its exact
request, pins and dispatch marker for `recover`, rather than another dispatch.
A repeated dispatch is rejected before a second remote launch. Only the selected
SDK/Python settings reach the remote profile; the complete input profile remains
in local mode-0600 files.

Campaign preparation now fails before creating an executable trial if the
PID-view proof is missing or invalid, and reports the initialization command
and expected `initialized-profile.json`. Initial proof records how to interpret
the device's process view; it is not a claim that the device is idle later.
Each actual native dispatch still checks current occupancy, mapping, activated
visibility and shared lease evidence. README documents this explicit workflow,
recovery and proof boundary, and removes the obsolete fixed 20-call framing.
An unset limit remains absent; explicitly requested limits remain effective.
The initializer's bounded receipt wait is not an operator/model budget.

The connected initializer regression passed 65/65, with no skips
(`.validation/device-isolation/initializer-connected-summary.json`). The
receiver's focused run passed 19/19 and the client run passed 11/11, retained in
`product-receiver-acceptance.json` and `initializer-product-acceptance.md` under
that same directory. These are overlapping local suites, not additive totals.
They use real local CLI/CPU subprocesses and synthetic original-byte proofs,
including an explicitly selected device 1; they did not initialize a real NPU
view. The client first run's sole failure compared a base policy to the policy
plus its returned proof; the corrected assertion uses the original view
artifact, without loosening product validation. Preparation's focused run
passed 50 tests with one optional private historical-receipt fixture unavailable
(51 total), recorded in `preparation-product-summary.json`; no old evidence was
rewritten to satisfy it.

The final complete Python invocation ran **621 tests** in **256.055 seconds**:
**619 passed, one error and one skip**, retained in
`.validation/device-isolation/python-final.log`. The sole error is the existing
`PreparedImportClosureTests` preparation fixture, which omitted the newly
required synthetic PID-view proof. Preparation correctly stopped before trial
creation. The only subsequent change supplied that synthetic proof to the
preparation fixture (four added lines, one removed); all original assertions
and production requirements remain intact. The complete pending-observation
module then passed **24/24**, recorded in
`.validation/device-isolation/pending-observation-final.log`. This resolves the
known failure within the tested scope, but it is a targeted module pass after
the 621-test full invocation, not a new complete 621/621 run. The Linux-only skip
remains separately covered by the real Linux CPU run.

The final TUI follow-up passed **7/7** at timeout scale **1.0**, with zero failed,
flaky or environmental groups. It covers deployment, authority, full JavaScript,
interaction, advanced, Goal review and Goal plan. The full npm result is
**225 passed, one genuine case-sensitive-filesystem skip**, zero failures
(226 total); authority passed **167/167**. The acceptance receipt is
`.validation/device-isolation/tui-final-focused/acceptance.json`, SHA256
`9ee81e547bfda62f3761f0f2e60b0dd70245208ed8f212b965f2456807c05c59`.
All **30** referenced artifacts independently matched their recorded lengths
and SHA256 digests. This is a seven-group follow-up after the previous complete
27/27 main-isolation invocation, not another full 27/27 run for the new entry.
No known test failure remains after the documented focused correction; the
failed complete Python invocation and earlier failures remain retained.

The next independent clean batch is planned in **min → max → matmul** order.
The two scatter operators are the primary tasks; Cube/matmul remains auxiliary.
Each starts without a candidate implementation, and no total model-turn or token
ceiling is installed. Calls and tokens remain counted. Complete sessions,
including failed attempts, source versions, raw receipts and diagnostic reads,
will be retained rather than only the best candidate. Existing full-session
archives and unresolved original handles remain preserved. This is a plan,
not a new NPU/model execution or result. There is no new successful clean optimization
run, independent stable max reproduction or new min/Cube qualification
established by these local checks.
