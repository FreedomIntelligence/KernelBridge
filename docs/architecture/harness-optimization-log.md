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
