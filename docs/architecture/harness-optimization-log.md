# Harness 20 model-turn optimization log

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
  paging the reference; a receipt fetch returned 29,707 characters with source
  duplicated in content and structured data. This is a context-retention issue,
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
