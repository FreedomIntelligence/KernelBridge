# TUI and subagent acceptance — 2026-11-11

Current scope: native Pi TUI, concurrent/background subagents and local host recovery in `/Users/jerry/.codex/worktrees/harness-20turn/KernelBridge` (`codex/harness-20turn`). The core behavior below passed. This is combined evidence from separate runs; it is not a claim that every historical acceptance scenario passed in one run or that all original redesign checks are complete. [Redesign §6](../../docs/architecture/tui-subagent-redesign.md#6-实施与验收核对2026-11-11) records the remaining checks and adopted design changes.

Context-retention follow-up: successful outputs now remain available until the
owner-composed request approaches its authoritative byte limit. Native Pi
shell/extension/fork/compact messages use the SDK's conversion before trusted
prefix and receipt-index checks. Final verification: 15 directed checks;
86 npm tests plus types/managed checks (two original-artifact replay cases skipped
in discovery and passed separately); codegen 10 checks / 35 ordinary turns /
48 provider requests, advanced 11 checks, and parallel/background 8 checks.
Original evidence is `.validation/context-retention-{codegen-r3,advanced-r2,parallel-r2}`.
Python discovery had 185 passes, one optional skip and one portable-import error;
the portable module's two tests passed after the native conversion correction.
Failed/interrupted initial runs remain preserved in `.validation/`; this is
combined verification and does not imply a device optimization result.

| Current check | Verified result and evidence under `.validation/` |
| --- | --- |
| JavaScript and types | 65 tests passed, with TypeScript/managed-continuation checks: `harness-npm-r1.log` |
| TUI unit checks | 35 passed: `harness-tui-unit-r1.log` |
| Python and Torch | Base discovery: 155 passed, 2 skipped, 5 Torch import errors. Torch-enabled follow-up: 20 passed, 1 module-path import error; corrected precision follow-up: 3 passed. Original logs retained: `harness-python-suite-r2.log`, `harness-torch-unit-r1.log`, `harness-torch-precision-r2.log`. These are separate runs, not a single all-green discovery result |
| Concurrent/background PTY | 8 checks passed: `subagent-parallel-final-r3/scenario.json`. Journal proves child-model overlap and ordered results. A gated background child stays running while both its original parent input and a second parent prompt complete. Report expansion, `/agents`, tool rows, completion continuation and Esc followed by a new prompt also pass |
| Real DeepSeek | Five behavior stages passed across **two fresh sessions**, indexed by `subagent-final-validation.json`: simple parent lookup and natural automatic delegation in `subagent-live-final-r3`; explicit two-child evidence return, user prohibition of delegation and two background children with parent synthesis in `subagent-live-remaining-r1/scenario.json` |
| Real background result | Parent returned while both children remained active; two distinct durable completion notifications delivered once, followed by a parent answer containing both newly generated evidence markers. Final pending calls were empty; omitted budget remained `null` with zero reserved credits. This exercises the real `deepseek-flash` route and local files, not NPU evaluation |
| Narrow terminal and selected stop | Historical 48-column real-Pi fixture evidence remains in the **old checkout** `/Users/jerry/Desktop/KernelBridge/.validation/subagent-panel-pty-r5` (6 checks) and `subagent-stop-pty-r2` (3 checks). Covers three background children, 3→2→1 waiting counts, report updates, notification/resume deduplication and selected-child stopping. These were not rerun in the new tree |

The first live session's later explicit-delegation assertion failed because ambiguous marker wording was interpreted as technical keywords. That assertion is **not** counted as passed; the remaining stages ran in a fresh session with explicit marker wording. Failed runs and their Journals remain available. The final index states this session boundary; it does not turn the earlier failure into a full-session pass.

Implemented design differences: default concurrency is 6 with optional limits otherwise unset; no implicit budget is created. Background results use durable agent-originated follow-up inputs, `agents_status` and `stop_agent`, rather than a blocking `wait_agents` tool. Esc cancels current foreground work; selected background work can be stopped separately. Known effects settle before cleanup, unknown effects remain fenced, and resume does not replay original effects. Tester commands run in the shared project only in the foreground; writable background workers use Git worktrees and retain branches/diffs for parent review.

Outstanding original-plan validation: the complete success/failure PTY matrix for every native tool, live DeepSeek thinking-mode display, and the remaining historical linked-worktree/session matrix beyond the current advanced and ordinary-codegen runs. Existing protocol, boundary and selected PTY tests do not stand in for these checks. NPU compilation/correctness, eager-NPU baselines, profiler/IR device evidence and optimization-round results are separate campaign records; **this TUI acceptance makes no NPU performance claim**. No Linux/cross-host or all-provider parity is claimed.

## Historical local TUI acceptance — 2026-11-10

Status: local deterministic acceptance passed on macOS, Node 22.19.0 and Python 3.12. The implementation was completed before test authoring. Device qualification was initially deferred. The later supplied SLAI transport now reaches the authorized NPU target, and device discovery completed. Scatter optimization rounds and eager-NPU qualification are still pending; the discovered torch scatter_reduce fallback to CPU is not an NPU baseline.

## TUI and subagent redesign follow-up (2026-11-11)

Scope and design: [TUI and subagent redesign](../../docs/architecture/tui-subagent-redesign.md). Branch `feat/tui-subagent-redesign`, merged to `main`. Implementation preceded the new checks; failures found during acceptance were repaired and the affected checks rerun. Results below are combined from the recorded runs, not one simultaneous run. Local fixture replies are not live model or device acceptance; the DeepSeek row is a real, billable model run.

| Check | Observed result |
| --- | --- |
| Python unit tests (`test_subagents`, `test_workspace_discovery`, `tui/test_authority`) | 38 passed: concurrent delegation batch plan, mixed replies keep the source-order chain, no default per-child limits, tester commands without host credentials, worker edits only its worktree branch, Claude Code-style definition files, reasoning stream, credential-prefix holdback |
| JavaScript | 39 passed (including TypeScript and managed-continuation checks); session discovery and presentation 9 passed (tool rows, delegate progress, footer, exploration grouping) |
| Concurrent subagents PTY (`subagent_parallel.py`, default `regular` mode) | 7 checks passed: two children visible together; Journal shows the second child admitted before the first settled; reports via Ctrl+O without receipt JSON; `/agents` types and runs; tool rows; background child notifies and the parent continues; Esc cancels both children and the parent continues. Also passed with `KERNELBRIDGE_TUI_MODE=regular` before the default changed, and in `fullscreen` during development (those earlier artifacts were removed to free disk space) |
| Existing PTY regressions | `advanced_scenario` 11 checks, `subagent_cancel_scenario` 4, `codegen_edges` 2, `resume_default_home` 7, `goal_review_scenario` 5 and `pty_scenario` passed; `interaction_scenario` passed after pinning `fullscreen`; `codegen_scenario` (35 turns) passed 10 checks (35 additional turns, 48 local model requests; idle start, command gate, compaction/new, interrupt, opt-in Goal, provider failure, resume without replay) with `--timeout-seconds 600`; two earlier attempts timed out while concurrent workloads drove the host load average to 27–117 (per-step 90 s and the 10 s status socket), not on a product assertion |
| Live DeepSeek subagents (`subagent_live.py`) | 5 checks passed. For "audit two independent subsystems" the model launched two `kernel-reviewer` children in one reply and their model calls overlapped in the Journal; the explicit two-child request also ran concurrently; a simple lookup stayed in the parent; "no subagents" was respected; no file changed |

Original artifacts: `.validation/redesign-r4/` (regression batch), `.validation/redesign-r5/` (interaction and live reruns), `.validation/subagent-parallel-r9/`, `.validation/subagent-parallel-regular-r1/` and `.validation/redesign-r8-codegen/`.

Failures found and repaired during this follow-up:

- **Startup race**: input submitted before startup finished was sent twice, which caused `Pi input has no durable host receipt`.
- **Stream delay**: streaming held back a full credential length of text.
- **Delegation chain**: independent delegate calls formed a source-order chain, so the second child could not start before the first settled.
- **Connection blocking**: the host connection reader blocked on a running effect.
- **Shared admission context**: concurrent requests could record each other's call id.
- **Schema timing**: the `delegate` schema changed after the Gateway sealed it.
- **Executor blocking**: long child commands blocked the executor thread.
- **Checkpoint blocking**: a background child blocked conversation checkpoints.
- **Goal pause race**: `/goal pause` could withdraw an automatic continuation that Pi had already accepted, which surfaced as a transport failure. The continuation now stops without calling the model.

Test and prompt updates:

- **Delegation prompt**: the first live run reviewed both subsystems without delegating; the guidance was strengthened.
- **Outdated assertions**: scenarios that asserted the old tool-row text, the old `/subagents` text or Pi's `v1.5.0` header were updated.
- **Fullscreen pin**: `interaction_scenario.py` now pins `fullscreen`, because it exercises in-app history scrolling.

Not claimed:

- NPU or device results, Linux qualification, or feature-for-feature parity with Claude Code or Codex.
- Background children surviving their interactive worker: a background child ends with that worker and is recorded as interrupted.
- Copying untracked files into worktrees: `kernel-worker` requires a Git project and snapshots tracked changes only.

Original artifacts: `.validation/tui-20261110-final/` in the checkout. The portable [result index](results/local-20261110.json) records check exits, durations and original-artifact SHA256 values. Reproduce with the [self-contained runner](README.md).

## Default session history follow-up

The default `/resume` path incorrectly derived its registry from Pi's temporary `HOME`. Earlier PTY tests always set `KERNELBRIDGE_SESSIONS` and missed this failure. Discovery now uses the authenticated host's registry; the CLI also passes that resolved registry to its worker. Empty startups no longer outrank meaningful sessions in `--continue`. A second regression exposed that Pi's generic `SelectList.setFilter` matches IDs only: the CLI and in-TUI pickers now share explicit title/path/ID search and activity labels.

The [resume result index](results/resume-20261110.json) binds the implementation and original evidence: five discovery/search tests, seven real-Pi PTY flows with neither registry nor application-home overrides, and all 39 JavaScript tests plus TypeScript/managed-continuation checks. The PTY covers offline original-host recovery, Chinese search in both entry points, current-session visibility, saved branch and journal size, project/branch keyboard filters, project/all-project selection, cancellation, retained files, no replay and continuing past an empty startup. Worktree filtering is covered by the picker test with common-directory identities; the PTY does not create a linked Git worktree. The user's existing operator conversation was independently found in the real CLI picker; that check cancelled without attaching or executing. Local fixture replies are not live model or device acceptance.

## Fresh Goal review follow-up

The optional fresh completion evaluator is now implemented for project Goals, closing the limitation noted in the earlier operator/session acceptance below. It assesses original receipts in a separate tool-free model request, shares all existing usage limits, preserves completed evidence, and honors a user pause or replaced objective. It does not certify NPU performance. Ordinary startup and ordinary prompts do not trigger it. The persistent SDK now advertises its locally qualified streaming, native-fork and persistent-session capabilities; the legacy finite SDK retains its separate flags.

Eight reviewer tests passed (original evidence/usage, deduplication, malformed verdict, pause, replacement, unknown effect, forged completion and completion-request gating). A real native Pi PTY passed five flows including continue-then-complete and pause during the reviewer request. The 39 JavaScript tests and TypeScript/managed-continuation checks passed. Original artifacts are `.validation/goal-review-unit-r3.log`, `.validation/goal-review-pty-r3/` and `.validation/goal-review-npm-r1.log`; these use a local provider fixture, not a live NPU. The later requested scatter campaigns require eager NPU comparison and are recorded separately from this functional acceptance.

## Operator workflow and session follow-up

The [operator/session result index](results/operators-20261110.json) records the final implementation hashes and original local evidence. Features were implemented before new tests; failures discovered during acceptance were repaired and the affected checks rerun. The full regression batch and subsequent targeted runs are combined evidence, not one simultaneous run of every historical test.

| Check | Observed result |
| --- | --- |
| Full local regression runner | All 15 groups passed, including 35-turn ordinary codegen, 20-turn compact/fork, SDK, recovery, steering, lifecycle, transport, admission and PTY |
| Extended native TUI | 11 flows passed: model-invoked pipeline checks/diagnostics, real community children, count rejection, failure recovery, manual fork suggestion, semantic compact, current fork, Goal idle/block/resume, original-host resume and session picker |
| Isolated historical fork/resume | 6 assertions passed: matching source restore, original session identity, no replay or extra Trial, retained ledger, rejected unknown entry, immutable reference |
| Subagents | 11 real-Pi tests passed: analysis/read-only, denied writes, per-turn/total/step/tool/budget limits, deadline, cancellation and unknown-receipt fencing; native Esc interruption followed by a new parent prompt also passed |
| Goal and host authority | 24 tests passed, including unlimited defaults, per-Goal token/model accounting, stale completion rejection and project fork limits without implicit Trials |
| Operator pipeline | 11 tests passed: 12 real CPU max/min cases, independent oracle, no-Torch bootstrap, incumbent/ABBA, complete/positive pair qualification, source/artifact binding, original-handle collection, uncertain-launch recovery and real IR/ISA/profiler |
| Shared infra | 9 tests passed covering all 17 tools; shared knowledge/run records remained unchanged. CANNBench HTTP is mocked GET-only; DQ/Forge checks use their supported fixture formats |
| Registered business tools | 4 tests passed through real Pi and the Journal; optional diagnostic/infra tools are callable, disabled delegation is absent and reserved names are rejected |
| Torch compatibility | 4 tests passed against installed torch 2.15.0 / torch_scatter 2.1.2 on CPU, covering broadcast/dim, empty bins, out/noncontiguous inputs, extrema and invalid inputs |
| JS and type regressions | 39 tests plus TypeScript and managed-continuation checks passed |
| Session discovery and configured startup | Offline title/activity discovery passed. The opt-in user profile registered 27 tools and started idle with 0 model calls and 0 device calls |

Newly exposed failures were corrected: stale Goal completion reason, Goal tools missing after awaiting-user, empty native fork, project fork limits accidentally depending on Trials, child tool-limit double enforcement, Esc leaving a delegation unsettled, and collection losing the original source after contract changes. Launch timeouts now retain an unresolved intent across new action IDs and contract changes; the recovery test dispatched once and recovered the original handle without another launch. Unknown child effects remain unknown and cannot be replayed or disguised as a known cancellation.

The no-Torch path separates correctness oracle, optional performance baseline and accepted incumbent. First complete correctness establishes an incumbent with `speedup: null`; subsequent comparisons explicitly select it as baseline. The paired-promotion fixture's 100/99 ratio is a protocol check, not a measured optimization claim. CPU diagnostics and generic driver protocols are locally verified; AscendC source candidates are included, but NPU compilation/correctness/performance and official calibration remain pending by user choice.

At this earlier checkpoint, the community `pi-subagents@0.87.0` component supported sequential foreground analysis/read-only children only, and a separate fresh completion evaluator was not yet implemented. The current concurrent/background, tester and worker behavior is qualified above; the Fresh Goal review follow-up records the later evaluator. The component remains a community plugin, not an official Pi built-in, and exact Codex/Claude Code feature parity is not claimed. See [workflow contracts](../../docs/operator-workflow.md), [TUI semantics](../../docs/tui.md) and [reproduction commands](README.md).

## Native Pi startup and official DeepSeek follow-up

The default entry now starts Pi 1.5.0's actual `InteractiveMode`, including its native header, theme, editor, completion, scrolling and keyboard handling. The earlier custom startup screen has been removed. Missing-model setup also uses `InteractiveMode`. A configured session starts a passive host immediately, with zero model or tool execution until explicit input; no Goal, Trial, seed file or workspace-history initialization is created at startup. An omitted budget remains unlimited and absent from the footer.

The [native Pi and DeepSeek result index](results/native-pi-deepseek-20261110.json) binds the current source and original artifacts. DeepSeek official configuration uses the `ds` / `deepseek-flash` and `ds-pro` / `deepseek-v4-pro` routes. Credentials are loaded from the private user file (mode 0600) or an overriding environment variable, including after host restart. No secret values enter project configuration or the evidence index.

| Check | Observed result |
| --- | --- |
| JavaScript and native regressions | 39 passed; TypeScript and managed-continuation checks passed |
| Native Pi codegen in a real PTY | 10 flows passed, 16 local fixture model calls; native idle startup, `/new` and shell before first prompt, direct read/edit/bash, consecutive input, compaction, interruption, explicit Goal, provider failure and resume |
| Missing model and explicit budget | Native setup/help/clear/exit and one-credit exhaustion passed |
| Official DeepSeek in a real PTY | 5 live model calls passed: read/edit/bash, completion returns idle, route selection, host restart from a fresh environment, no prompt replay and a resumed live response |
| Credential separation | Private file permissions verified; exact secret absent from checked source files and validation artifacts |

Final artifacts are `.validation/native-pi-unit-final.log`, `.validation/native-pi-final/`, `.validation/native-pi-setup-final/` and `.validation/deepseek-native-final-r3/`. The live check starts with `DEEPSEEK_API_KEY` removed from its environment and reads the user's private file. Pro route selection was checked; Pro inference was not run. The live configuration uses non-thinking mode.

Failed attempts remain available: the first older live-session restart correctly rejected changed runtime source; earlier PTY assertions expected a literal Pi logo or matched a prior `/new` receipt; two live harness attempts used an overlong socket path or incorrectly required a stale socket to disappear after SIGTERM. Final checks use the corrected assertions and the same current runtime across restart. No NPU, Linux/cross-host qualification or publication is claimed. The historical sections below retain their original run boundaries; their earlier no-host and no-real-model statements do not describe this follow-up.

## Earlier ordinary codegen follow-up

At this earlier checkpoint, the root `./kernelagent` opened a custom idle codegen editor in the selected project. No model request, host session, task scaffold, Goal or Trial was created until the user submitted input. The native Pi follow-up above supersedes that startup implementation. File and shell tools operate on that project. Omitted credit, step and Goal limits remain unset; the footer shows no budget. Goal scheduling and isolated device/evaluator workflows require explicit activation.

The [codegen result index](results/codegen-20261110.json) binds the final source, frozen Python modules and original artifacts. The canonical source-development launcher was exercised directly, including restart through `--continue`. This follows the user's revised ordinary-codegen contract; it does not claim feature-for-feature parity with other coding products.

| Check | Observed result |
| --- | --- |
| JavaScript, native tool, command and environment checks | 38 passed; TypeScript and managed-continuation validation also passed |
| Journal, lease, stream and codegen budget/control checks | 17 passed, including unbounded accounting beyond 200 credits and completed Goal controls |
| Existing real Pi/Executor regressions | 7 passed |
| Ordinary codegen in a real PTY | 35 additional user turns, 48 total local model requests; exact ordinary-call count, direct read/edit/bash, large project content, Chinese input, compaction, new conversation, interrupt, opt-in Goal, provider failure and resume passed |
| Missing model and explicit budget | Setup/help/clear/exit remained usable; one-credit initial-prompt limit stopped without editing, overspending or pending reservations |
| Actual default manual demo | Idle first, read/edit/bash, `!node --version`, consecutive user shell commands, clean tool output and exit passed |
| Final SDK regressions | Read/edit/evaluate, steering, worker/host recovery, provider failure and admission pause passed |
| Goal lifecycle | Pause, detach-continue, awaiting-user and three explicit-limit cases passed |
| Experiment-mode rendered TUI | Completion, Goal controls, visible command rejection, history navigation, latest submission and narrow footer passed |

Original final long-run artifacts are in `.validation/codegen-final-35/`. The short completion/clear/resume check is in `.validation/codegen-goal-clear-complete/`; the final SDK and lifecycle artifacts are in `.validation/codegen-sdk-complete/` and `.validation/codegen-lifecycle-complete/`. The manual demo is `.validation/codegen-demo-verified-r2/`. Reproduce using the [validation commands](README.md).

Failures were retained and corrected: short-context compaction left the editor populated; a queued/native input race duplicated delivery; a late interrupt could reopen a completed Goal. Test harness corrections distinguish retained historical Goal Trials from newly created Trials, and wait for the actual editor rather than the demo's pre-start banner. The final 35-turn run passed the corrected assertions. These are combined recorded runs; earlier failures are not presented as passing evidence. The isolated 20-turn SDK result remains from the earlier regression batch; final checks cover the changed input/control paths.

No real model endpoint or NPU was used. Vector/Cube device acceptance remains pending by the user's instruction; Linux/cross-host qualification and publication are not claimed. Existing terminals launched from an old installed bundle must be exited and reopened from the root launcher to use this implementation.

## Interaction follow-up

The [UI follow-up index](results/ui-20261110.json) records acceptance across the retained runs for the final runtime. It includes 43 unit/regression checks (13 authority, 23 Pi/tool/environment, 7 existing Executor), TypeScript, the seven SDK/host scenarios, and the original PTY scenario. A clean installation at `.validation/tui-install-ui/bundle` also passed the rendered-screen PTY checks: `/goal TEXT`, pause/resume/clear, actual Tab completion, visible rejection of unsupported commands, history-to-latest submission, Chinese input and a 48-column footer. Goal policy JSON and synthetic host-stop prose no longer appear as model output.

The deterministic admission test pauses after `prepare_turn`, before atomic model admission: no model runs or budget is spent at that boundary, no unresolved call remains, and the same worker resumes to three model calls and a full E1 fixture receipt. Python payload digests match across the two regression deployments and the installed runtime.

These results are combined from recorded runs, not a claim that the last parallel batch was entirely green. `.validation/tui-ui-regression-r2` passed all checks except an admission-test assertion that incorrectly treated a mapping as an array; the corrected assertion passed in `.validation/tui-ui-final`. That later parallel repeat hit resource-related timeouts in long-session, lifecycle and PTY checks; their earlier passes on the same runtime and all timeout logs are retained. The installed screen test passed in isolation with a 90-second per-condition deadline. The runner now defaults to one job, and PTY cleanup reaps its own stalled process group.

No real model endpoint or NPU was used. The prior acceptance tables below remain historical records of their stated runs.

| Check | Observed result |
| --- | --- |
| Journal/input/lease/evidence/stream unit checks | 13 passed |
| Pi patch, native tools and environment checks | 18 passed |
| Existing real Pi/Executor regressions | 7 passed |
| TypeScript | Passed |
| SDK read/edit/evaluate | 4 model requests, one E1 fixture receipt, 15 credits |
| Real PTY | Chinese multiline input, resize, long output, `!`/`!!`, reload/model, metered compaction, reset and terminal restoration passed |
| Long session | 20 additional user turns across worker lifetimes; compaction and fork; 28 total model calls, 39 credits, 2 Trials |
| Worker/host failure and truncated Pi JSONL | Original evaluator ran once; resumed with 4 model calls and 15 credits |
| Steering/follow-up/interrupt | Inputs persisted once, busy prompt rejected, interrupted input resumed through an explicit new turn |
| Background lifecycle | Pause, detach-continue and awaiting-user behavior passed; automatic Trial rollover stopped at 5 model calls and 3 Trials |
| Provider failure | One failed call, no automatic retry; explicit new input continued with a new action ID; 5 total model calls, 16 credits |
| Supplemental CPU, retained candidates, RSI snapshots, portable replay, installer and native-failure regressions | 15 passed |
| Clean-directory installation | Locked dependencies and all four Python packages installed successfully; the installed CLI accepted its runtime bindings |
| Installed CLI in fresh terminals | Exit/attach retained the same Pi session and Trial; budget continued from 24 to 25 credits; both terminal shutdowns passed |
| Installed host lifecycle and exhausted credits | All six cases passed; 1-credit initial-file denial and 7-credit model exhaustion both ended at `budget_limit`, with no remaining reservation |

The baseline evaluator is a small local Python fixture. E1 here means that fixture's full correctness contract; it is not an NPU result. Recovery fault injection includes an accepted local evaluation with its worker reply lost. It does not substitute for the remote NPU submission-key test.

Implementation review also corrected undeclared interactive plugin activation, bash writeback after failure, detach policy propagation between control and effect connections, stale-socket error propagation, worker-exit/lease-release races, provider-failure input status, cross-lease starts, incumbent selection across branches, and budget-denied initialization being mislabeled as awaiting user input. Unknown effects keep their reservations; only provably unstarted admissions can be cancelled and refunded.

The [installation and supplemental index](results/installation-20261110.json) binds these additional checks to their original logs and final runtime source hashes. The installed-entrypoint test includes the final Pi patch: exit hints use `kernelagent tui --attach`, and native project-trust guidance cannot offer an inapplicable resource-loading path. It waits for the host footer before submitting after attach, because Pi enables raw terminal mode before its submit handler is ready.

Real Linux execution, different-uid connection attempts, cross-host restore, Vector/Cube NPU evaluation, device 20-turn runs, capability promotion and publication remain unqualified. Prior delivery/device results are historical and are not reused as evidence for this implementation.

## TUI polish and live trajectory viewer (2026-11-12)

Commits `3a4d142`, `6dc9a79`, `9d98de8`, `5af7700`. Plan and per-item findings: [tui-polish-plan.md](../../docs/architecture/tui-polish-plan.md).

Presentation changes were found by rendering real screens from the PTY scenarios and comparing against Claude Code and Codex, not by assumption: one blank line between transcript rows (the Pi patch now trims the user component's own padding), one two-column `/help` table for both workspace modes that fits a 24-row 48-column terminal, marked host feedback (`⏺` notice, `✗` error), a footer that hides zero counters and elides a long path in the middle so the directory name and git branch stay readable, and tool rows that always name their arguments. The working row counts messages queued behind the running turn, and an interrupted turn leaves a visible row instead of ending mid-thought.

`/trace` and `kernelagent trace` serve the vendored Lattice Trace viewer from a read-only projection of the session Journal, updated over SSE. The server binds 127.0.0.1 behind a random path token and checks the `Host` header; the trusted CLI process hosts it, and the TUI worker only receives the URL. Viewer transforms are exact-match and fail loudly if the vendored file changes.

Checks: `subagent-presentation.mjs` (8), `interaction.mjs` (5), `trace-projection.mjs` (5), `trace_scenario.py` (5, including a headless Chrome render with no page errors), `codegen_scenario.py` (10, including `interrupt_then_continue`), `interaction_scenario.py` (including a 48-column screen), `pty_scenario.py`, `advanced_scenario.py` (11), `subagent_cancel_scenario.py`, `codegen_edges.py`, `resume_default_home.py`, `goal_review_scenario.py`. `install.py --verify-only` clean.

`pty_scenario.py` and `advanced_scenario.py` first failed by timeout, never by assertion, at a different step each run, while unrelated processes held this workstation's load average between 18 and 70 with the data volume full. Both pass when run without contention; the earlier failures are recorded as environmental, not as passes.

Not qualified here: the plan surface (`/plan` or todo) decision, an editor placeholder, a narrow-width sweep of every host-rendered block, and the O(journal) read-only `status` path noted in the plan.
## Model picker and reasoning levels, startup, provider retries and subagent timeouts (2026-11-12)

Branch `feat/model-reasoning` with `perf/startup`, merged with `main` at `d07af47`. Alignment targets: Claude Code's `/model` (←→ effort) and `/effort`, Codex's model/effort choice and `request_max_retries`, and both tools' handling of subagent and provider failures (research brief: third-party guides and GitHub issues plus the AWS retry guidance, since official pages could not be fetched here).

| Run | Code | Result |
| --- | --- | --- |
| `acceptance-model` | `b95a093` | 26 passed, 1 failed (`codegen`: `--continue` after retried calls raised "action ID reused with different inputs"); not counted |
| `acceptance-model2` | `2241a5a` (recovery follows retry chains) | 27/27 passed, timeout scale 1.0 |
| `acceptance-model3` | `4ee472f` (merged with `main` `d07af47`) | **27/27 passed**, none flaky, timeout scale 1.09 |

Found and repaired on the way:

- **Thinking mode with tools was unusable.** Live on DeepSeek, the first tool-call follow-up with thinking enabled returned HTTP 400 ("The `reasoning_content` in the thinking mode must be passed back to the API"), even when the model had produced no reasoning, and the session silently fell back to the next route. Reasoning now stays in the settled reply and the Pi transcript, passes the projection and its validator, and is sent back on every assistant message of the current turn (empty when there was none). Verified live on `deepseek-flash` at `low` and `high`, including 114 characters of reasoning sent back across a `bash` call.
- **Resume after automatic retries.** Recovery matched only base attempt IDs and tried to close an already ended step again. A step with an end record is now closed whichever attempt it names; `__model_retry__` successors map to their step through the chain's hash links.
- **Continuation label.** After the merge, the continuation facts no longer carried the Goal's limits, so `未设上限` would have shown for limited Goals; restored and tested.

Startup (perf fork, 3 runs each at load about 6): first paint 2.23–2.87 s → 0.16–0.36 s; idle prompt 2.35–3.01 s → 1.26–1.75 s. In-TUI `/resume` with 38 sessions 5.8–6.3 s → 2.2–2.8 s. Keystroke-to-repaint in the `/` menu and pickers was already 10–35 ms and is unchanged.

Other evidence: `npm test` 202 passed, 3 skipped (two original-artifact replays and one that needs a case-sensitive filesystem); Python `validation/tui` 132 OK, `test_subagent_recovery` 19 OK; the broader `validation/` discovery has only the 5 Torch import errors this environment always has. New tests: `test_model_reasoning.py` (23), `model-picker.mjs` (7), `test_provider_retry.py` (12), `provider-retry.mjs` (5), interaction scenario `model_picker_reasoning_level`, codegen `transient_provider_failure_retried` and `provider_failure_visible_next_input`.

Not claimed: reasoning levels for non-DeepSeek endpoints without a declared `reasoning_levels`; `@` file completion, which needs `fd` (not installed here); the pi-tui race where very fast typing can accept a stale autocomplete entry.

## Goal continuation limits follow-up (2026-11-12)

Found in a task-mode trial (`hongkong-observe-open-max-01`) with no model-call, token or credit limit: after 47 steps in one input the model ended a turn with "I've exhausted my budget for this turn" and no tool call. The request held no budget wording (no `goal_progress`, credits `limit: null`, no per-Trial limit); the Goal then continued automatically, as designed, with nothing telling the model or the user that no limit had been reached. Each automatic continuation now states every limit and its usage in words, the operator prompt states the no-ceiling case positively, the tool-free continuation stop applies in task mode too (an unlimited task-mode Goal could otherwise keep continuing a text-only model), and the transcript row reads `Goal 自动继续 · 已用 N 次模型调用 · 未设上限`.

Checks: `test_goal_continuation.py` (8: limits text with and without limits and after a Trial limit; continuation text and the no-progress stop in both modes), `interaction.mjs` (continuation label), `operator-task.mjs` (prompt wording), `npm test` 148 passed / 2 skipped, Python authority 87. `acceptance-limit2` on `737f0e3`: **27/27 passed**, none flaky, timeout scale 1.0. A first run (`acceptance-limit`) failed 9 checks at the delivery check because bytecode written by a local preview run sat under `source/`; it is not counted.

## Industry alignment, trace plugin, 进展 page and acceptance runner (2026-11-12)

Scope: [industry-alignment.md](../../docs/architecture/industry-alignment.md), [pi-trace-plugin-and-tui-plan.md](../../docs/architecture/pi-trace-plugin-and-tui-plan.md) and Phase 4 of [tui-polish-plan.md](../../docs/architecture/tui-polish-plan.md). The runner is `validation/tui/run.py` (schema v2). Each check ends passed, flaky, failed or environmental, and only passed counts.

| Run | Code | Result |
| --- | --- | --- |
| `acceptance-r1` | `07d5c02` | 25 passed, 1 flaky (`subagents-panel`); the runner then misread an assertion failure as a timeout, see below |
| `acceptance-r2` | `b229b85` (checkpoint race fixed) | 26/26 passed, timeout scale 1.64 |
| `acceptance-final3` | `33e7973` (merged with `origin/main`) | 26 passed, 1 failed (`interaction`: `git branch` raised outside a repository); not counted as a pass |
| `acceptance-final4` | `5bb40df` (fix, 进展 page, live `/goal`) | **27/27 passed**, none flaky, timeout scale 1.75, load per CPU 0.8–2.0; evidence hashed in `acceptance.json` |

Failures found and repaired:

- **End-of-turn checkpoint race.** Under load, the checkpoint taken at the end of a turn reached the host after a queued subagent notification's `__input__` had been admitted. The host refused it as unsettled, and Pi printed the raw stack into the transcript. Replaying the Journal of the failed run showed the window: the input completed at seq 151, `__input__` was admitted at 152, and no checkpoint came between. The fix finishes turn settlement before the next input is applied. With load average about 80, the old code failed on the first of three runs and the fix passed 3/3.
- **Runner classification.** A failure followed by a cleanup `TimeoutExpired` was treated as a timeout and retried. Outcomes are now taken from the first exception. Transcripts are also written when shutdown stalls; before, a stalled shutdown lost them.
- **Viewer.** The branch history drew NaN coordinates for a session with one lane, failed for a subagent without a return step, and the divergence summary failed when a branch began with an input. Browser checks did not see these because the viewer catches and logs them. The checks now count logged errors and cover all ten tabs.
- **进展 was disabled for live sessions.** The viewer's progress module had pages only for its demo studies. The trace now carries `progress` (Goal, accepted plan revisions, completion reviews, usage since the Goal started) and 进展 › 目标与计划 renders it. The Journal projection also skipped atomic rows, where the host writes Goal and review records, and showed the completion review's model call as raw JSON.
- **`/goal` status was a snapshot.** It printed `已用时 0s · 模型调用 0 · tokens 0` and never changed. The block is now live: elapsed time advances each second while the Goal runs and the counters follow each state refresh.
- **Tests already failing on `main`.** The `test_goal_progress` stub lacked `subagents`; `kernel-worker` is only offered with a committed HEAD; footer and working-row wording had changed; `test_operator_pipeline` picked a conda `clang++` without the SDK headers (now `/usr/bin/clang++` on macOS unless `CXX` is set).

Other evidence:

| Check | Result |
| --- | --- |
| `npm test` | 147 passed, 2 skipped (original-artifact replays that need external fixtures), 0 failed: snapshots 65 files, width properties 5×250 samples, OTLP, plan presentation and live `/goal` status, trace projection including progress, Pi plugin |
| Python authority tests | 79 tests OK in the `authority` check, including Journal read path 14, Goal plan 10, traceparent 2 |
| Journal read path | 856 ms → 116 ms per snapshot at load 166; 336 ms → 43 ms at load 22; `status` takes one read instead of five; independent review (no high-severity findings; writer starvation under `LOCK_SH` fixed) |
| `F_FULLFSYNC` | `fsync` median 0.02 ms (no flush), `F_FULLFSYNC` median 3 ms, p90 4.4 ms |
| Real Pi sessions (`pi_real_replay.py`) | 7 DeepSeek sessions from local runs, 227 steps: no errors, 101/101 input-token counts equal the provider's `prompt_tokens`, 3 largest render cleanly; only counts recorded |
| Branch views (`trace_branches_render.py`) | Pi session-tree branch with a subagent, and a KernelBridge `/fork` with a running child and a Goal plan: 10 tabs × 2 scopes, no errors; 进展 shows the Goal and plan for KernelBridge, the no-Goal note for Pi, and its step links select the step |
| 进展 on a real Journal | The `goal-plan` scenario's Journal: Goal, two accepted plans (the refused one omitted), the `met` review, curve and activity, no page errors |

Not claimed: NPU or device results, Linux qualification, publication of `pi-lattice-trace` (kept in-repo by decision), TRACEPARENT for Pi's native `bash` and user `!` commands, and xterm-based full-screen snapshots (component snapshots are used instead).

## Operator guidance and diagnostic feedback baseline (2026-11-12)

On `codex/operator-guidance-tools` after merging `39d9c9f`, the source-frozen
`.validation/operator-guidance-tui-r1/acceptance.json` passed **27/27** checks,
with no flaky, failed or environmental result (timeout scale 1.36). This includes
ordinary codegen, interruption, recovery, fork, compact, Goal review/plan and
parallel/background subagent interactions. Evidence and original logs are
hashed in that acceptance file. This is local deterministic acceptance, not NPU
qualification or operator performance.

Full Python discovery with the existing CPU Torch dependencies passed 359 tests
and skipped two optional original-private-receipt replays (361 total). Both
original replays subsequently passed separately. The initial discovery without
Torch had five import errors and four obsolete task-file/budget assertions;
those logs are retained. JavaScript passed 164 with three skips (167 total);
the two original-session replays subsequently passed with their original bytes.
The remaining JS skip requires a genuinely case-sensitive filesystem.

The original uncapped NPU trial was subsequently paused at the user's request
for complete session analysis. Newly discovered child-page truncation and
successful-build diagnostic visibility defects are follow-up work; this baseline
must not be represented as testing those later fixes.

## Final session-audit repairs and main integration (2026-11-12)

After merging main `0b14f8a` and completing the child-page and successful-build
diagnostic fixes, `.validation/operator-guidance-tui-final/acceptance.json`
passed **27/27**, with zero flaky, failed or environmental results, timeout
scale 1.0. Its `js` check ran the required full `npm test`: **165 passed,
3 skipped, 0 failed** (168 total). The skips require two optional historical
artifacts and an actually case-sensitive filesystem; the former had separately
passed with the original fixtures in the earlier recorded replay.

Final Python discovery passed **400/400**, zero skips, with existing CPU Torch
dependencies and the explicitly supplied original private replay fixtures.
Original logs are `.validation/operator-guidance-tests/python-final.log` and
`tui-final.log`. Four new modules cover child observation paging, original build
diagnostics, complete build feedback and control-acknowledgement recovery.
The paging replay passed 16/16 through 41 local HTTP mock requests and 32 visible
pages; no old candidate implementation was embedded into the test module.

The real compiler-only IR probe and the one original-job alias read are recorded
separately. Neither is a new candidate execution or performance qualification.
All earlier failed dependency/fixture runs remain retained. Following delivery,
these ignored artifacts are archived under
`.validation/operator-guidance-delivery-20261112/`; recorded original paths are
historical identities and are not rewritten.

## Instruction snapshot and continuation follow-up (2026-11-12)

The first source-frozen run at `codex/agent-guidance-parity`, recorded in
`.validation/acceptance/tui-r1/acceptance.json`, passed 26 of 27 groups at timeout
scale 1.0. The runtime group failed three worker deadlines under heavy load;
it is not counted as passed. All PTY groups, including parallel children,
completion notifications, stop, resume, fork, compact and Goal interaction,
passed. The full JavaScript check passed 190 tests, skipped three conditional
cases and failed none (193 total). Follow-up verification must retain this
failed attempt and use unchanged behavioral assertions and production limits.

The subsequent full Python run, `.validation/acceptance/python-r2.log`, passed
424/424 with zero skips in 227.755 seconds. It used explicit Node resolution,
the existing CPU Torch dependency path and original private replay fixtures.
The earlier 421-test run's failure, errors and skip are retained. The fixtures
now use production Node discovery and an already-launched original handle for
the zero-wait recovery check; production deadlines were not increased.

The focused `.validation/acceptance/tui-runtime-r2/acceptance.json` then passed
deployment and runtime (2/2, runtime 7/7 in 10.67 seconds). The runner selected
timeout scale 1.32 for its outer process deadline; no runtime assertion or worker
limit changed. Every required group now has a passing result across the full run
and focused follow-up; this does not describe a second full 27/27 invocation.

## Shared Journal admission stress follow-up

Four stress scenarios passed in `.validation/propose-admit-stress-summary.json`: 32 independent Gateway/Journal instances issued 320 proposals (192 original HTTP completions and 128 budget rejections); 64 concurrent copies of one ID dispatched once; parent admission survived 32 concurrent child proposals; 32 real child permits retained an unknown-result fence until original-receipt recovery. Every Journal prefix was checked for nonnegative reservations, no overspend and unique admission/start/settlement. All final reservations were zero. The first child fixture omitted its parent dependency and was correctly rejected; that failed fixture log remains retained. No runtime policy was weakened.

## Final ordinary and advanced TUI regression

`harness-codegen-final-r1/scenario.json` passed ten checks, including 35 ordinary turns without an implicit limit, compact/new, interruption, opt-in Goal completion, provider failure and original-session resume without replay (48 provider requests). `harness-advanced-final-r2/scenario.json` passed eleven checks, including registered A-J fixture calls, two children and count rejection, failure recovery, manual fork hints, semantic compaction, native fork, Goal blocking/resume and session picker cancellation. The earlier advanced run retained a child timeout on the loaded machine; its fixture deadline was raised from 15 to 120 seconds because this scenario tests count limits and recovery, while dedicated cancellation/deadline tests retain their original bounds. All behavioral assertions remain unchanged.

## Native terminal recovery and continuation acceptance (2026-11-12)

In `codex/harness-terminal-recovery`, the complete
`.validation/acceptance/tui-r2/acceptance.json` passed **27/27**, with zero
flaky, failed or environmental groups at timeout scale 1.0. Its 145 evidence
files were independently checked against the recorded byte lengths and hashes.
The authority group passed 121 tests; full `npm test` passed 212 tests, with
zero failures and one skip requiring an actually case-sensitive filesystem.
Both optional historical session replays ran with the original, read-only
fixtures and passed in this full invocation.

The full Python discovery in `.validation/acceptance/python-r2.log` passed
470/470, zero skips, in 309.526 seconds, using the existing CPU Torch path and
original private replay fixtures. Subsequent tool-free reason compatibility
changes passed 35 focused Python checks, 25 JavaScript checks and the complete
TUI run above. The isolated Linux CPU supervisor qualification passed six
checks with actual process births, exit codes/signals and lease inheritance;
its original output and uploaded source hashes are retained in
`.validation/supervisor-local/linux-collection.json`. It did not run an NPU
kernel or create an operator performance result.

Failed attempts remain retained. Python r1 had one failure among 469 tests:
the new opt-in observation protocol incorrectly rejected a legacy driver's
ordinary unknown ACK. The fix preserves the old nonterminal ACK behavior and
still rejects protocol downgrade and forged observations. The original test
expectation was unchanged. TUI r1 passed 25/27: the narrow interaction test
assumed a full long branch and `/help` could always fit together, and the
advanced test exposed an unnecessary public reason rename. The production
layout remains unchanged; the interaction test now verifies both existing
responsive forms, including the branch prefix, path and width. Tool-free
continuations retain `no_tool_progress`, while repeated tools without new
information use `no_executable_progress`; the advanced assertion is unchanged.

These results qualify local behavior and recovery contracts. The previous
clean max result is not a new trial of this revision. The original min job
still has no terminal receipt, Cube has not started in that batch, and stable
fresh device repetition remains outstanding.

## Rebase qualification and terminal-persistence follow-up (2026-11-12)

After rebasing the recovery branch onto main `c8a2e45`, full Python discovery
passed **470/470**, zero skips, in 270.806 seconds
(`.validation/rebase-main/python-full.log`). The earlier 469-test legacy-ACK
failure and subsequent successful 470-test run remain retained under
`.validation/acceptance/`; the rebase run is a separate full invocation.

The post-rebase full TUI run in
`.validation/rebase-main/tui-full/acceptance.json` passed **26/27** groups at
timeout scale 1.0, with one failed group and no flaky or environmental outcomes.
The sole failure was the full JavaScript check: 223 passed, one failed and one
case-sensitive-filesystem skip (225 total). Its live Goal elapsed assertion
observed `1m04s` for an exact 65-second interval. This failed full run is retained
and is not reported as passing.

The defect was production display arithmetic: converting epoch nanoseconds to
`Number` before subtraction could round a whole-second boundary downward.
The repair subtracts and divides using `BigInt`, converting only the small
elapsed-seconds result to `Number`. The old live-clock/widget assertions remain
unchanged; a fixed-clock regression was added. Deterministic probes found 125
wrong values in 1,000 timestamp phases with the old code and none with the
repair. The applied `plan-presentation.mjs` follow-up passed **6/6**, no skips,
in `.validation/goal-elapsed-draft/applied-targeted.log`. Host accounting,
budgets, layout and device timing did not change.

A separately identified process-persistence window is also repaired. When the
original child has exited but its original supervisor is still persisting the
wait receipt, verified `settling` permits collection of that same receipt.
It remains unknown and fenced; no replacement launch or early qualification is
authorized. Original launch/request bytes, pinned code and process birth must
match. Missing proof, a lost/dead supervisor or legacy liveness claims still wait.
The native lifecycle regression passed **45/45**
(`.validation/supervisor-settling/targeted-final.log`); projection, continuation
and original-identity regression passed **37/37**
(`.validation/terminal-recovery/settling-focused-final.log`). These are focused
checks after the full Python run, not a claim that an enlarged full suite ran.

The independent CPU-only Linux probe passed **7/7**, with terminal return code
zero and retained original output/source hashes in
`.validation/supervisor-settling/linux-collection.json`. Six checks use the
unmodified production module. The seventh uses an isolated test copy with a
file latch after the actual child wait to observe `settling`, then the validated
original exit receipt. No persistence delay was added to production. The probe
used its own temporary namespace and lease, without Torch, CANN, a compiler,
an NPU kernel or the original min deployment.

The final focused TUI run passed **7/7**, with zero failed, flaky or
environmental groups at timeout scale 1.0. It covers deployment, full JavaScript,
authority, interaction, advanced, Goal review and Goal plan. Its full npm run
passed **225 tests**, with one case-sensitive-filesystem skip and zero failures
(226 total); authority passed **162/162**, and advanced passed all 11 checks.
All 30 referenced evidence files matched their recorded lengths and hashes.
The acceptance file is
`.validation/rebase-main/tui-final-focused/acceptance.json`, SHA256
`fa1b001605f3dd92423f76743d52c951dc261eb53eab525da70dfd292fb754ae`.
The earlier pre-rebase full run's 145 evidence files were also checked and remain
retained. This qualification combines the post-rebase full run's 26 passing
groups with the focused follow-up; it is **not** a new full 27/27 invocation.

These checks qualify harness behavior. Max remains the single previously
accepted paired result; min's original job remains unknown, Cube has not started
in that batch, and fresh stable device repetition remains outstanding.

The final original-min read-only identity check at
`.validation/original-min-final-recheck/recheck-report.md` still found no
benchmark result or actual exit receipt. Original source, intent, launch,
intermediate status and local receipt bytes remained unchanged. An absent PID
and free lease do not establish an exit code or benchmark verdict; the old
persisted `running` checkpoint is not evidence of current liveness. The original
attempt remains unknown, and the new supervisor cannot retroactively provide
its missing exit evidence. A subsequent clean batch is planned with an
independent session and namespace while preserving that unresolved original;
it must not redispatch the old handle. This entry records no new batch launch
or optimization result.

## e19 clean-min feedback audit and native-failure-binding follow-up

The earlier terminal-recovery delivery was merged and pushed to main as
`e19c804`. A later clean min session is retained in the
`harness-terminal-recovery` checkout under
`.validation/next-clean-preparation/trajectory/observations/min-7eec272bd535fc56/`.
It used 37 settled calls (30 parent, seven child), 1,687,106 reported tokens and
one actual native attempt. It has no F/G/H or E1/E3, and no explicit J or actual
profiler/IR-content read. These are observational results, not passing operator
acceptance or evidence of improved optimization efficiency.

The original benchmark report and supervisor exit establish a native compile
failure and return code 1. An independently captured original run-log receipt
binds the benchmark digest, but the local supervisor-failure projection omitted
that digest. Strict log verification raised `Original run log binding changed`,
and the outer evaluate incorrectly returned pending with a null original handle.
The specific compiler diagnostic did not reach later model requests. This new
known failure must not be conflated with the older d07 min attempt, for which
original terminal evidence remains absent and unknown is still the correct
state. Neither path authorizes replacement of the old handle.

The same audit identified two independent instruction defects: a hardcoded
`max_trials=1` was interpreted as a one-candidate budget, and a read-only child
was told to use an unregistered `finish` tool. The final stop was the model's
explicit `update_goal(blocked)`, which the host honored as `awaiting_user`;
it was not an automatic missing-receipt pause. The child recovered with text
after an extra model call. The new repair round addresses source/handle-bound
failure/log projection, unspecified versus explicit campaign budgets, and
registry-aware child completion guidance. It does not add operator answers,
relax identity checks, or alter suite/oracle/timing contracts.

The first full Python invocation ran **487 tests**, ending with **three
failures, two errors and one skip** in
`.validation/acceptance/python-full.log`. The failures/errors all came from
removing the existing nullable `application.max_model_turns` field. That schema
compatibility is restored without installing a numeric budget. The skip was due
to a missing local link to the existing shared scatter dependencies; after the
link was restored, the DQ check passed separately (**1/1**). The optional
historical max/Cube receipt check also passed separately (**1/1**) in
`.validation/failed-benchmark/historical-receipts.log`. These separate checks do
not turn the failed first full invocation into a pass.

The first full TUI invocation passed **26/27** groups at timeout scale **1.02**,
with one failed group and zero flaky/environmental groups. The sole interaction
failure was an assertion requiring the complete directory basename when the
long branch name legitimately shortened the footer. The assertion now checks
the precise allowed shortened prefix; production UI behavior was not changed.
All **144** referenced evidence files matched their recorded hashes. The failed
full-run receipt is `.validation/acceptance/tui-full/acceptance.json`, SHA256
`aa74b0380c50d55eb2cb4e6cac6f2787ca2e625364da8769669519a4a6d807c0`.
The `get_goal` projection also exposes `policy.max_steps` in a separate scope,
keeping it distinct from an absent aggregate Goal budget.

The rebuilt second full Python invocation passed **487/487**, with **zero
skips**, in **229.800 seconds** (`.validation/acceptance/python-r2.log`). The
final focused TUI invocation passed **7/7**, with zero failed, flaky or
environmental groups at timeout scale **1.0**. Its full npm run passed **225**
tests with one genuine case-sensitive-filesystem skip; authority passed
**167/167**. All **30** referenced evidence files matched their recorded lengths
and hashes. The receipt is `.validation/acceptance/tui-final-focused/acceptance.json`, SHA256
`d2343ecb80f4ce860c859e7d4927ef2b23db63a3eaa91ee3894e15fb31ea64a7`.

This TUI qualification is the first full invocation's 26 passing groups plus
the seven-group focused follow-up, **not** a new full 27/27 invocation. The
failed first Python invocation and its missing-dependency skip remain retained,
as does the original TUI assertion failure. The second full Python invocation
includes the restored dependencies and has no skip. Multiple independent
changes preclude a single-factor explanation of any later difference in
model-call count. No successful optimization run on this repaired version is
reported; a new independent clean trial remains pending.

The best max session was independently archived at
`~/.kernelbridge/optimization-archives/scatter-max/d07af47-application-f44e42d3-bf1f-4add-963a-2719fda4202f`:
589 original files were copied byte-for-byte and its final 601-file manifest
verified. All four native attempts, failures and the full conversation are
retained. A read-only integrity review found no identified reward-hacking
behavior and reproduced the original receipt/timing consistency; this is not a
new NPU run or a proof against arbitrary native interference. The six-case
5.250296× result remains a single qualified same-device eager comparison,
without independent stable reproduction/E4 or successful-candidate profiler/IR
evidence. This entry records no new successful min or Cube result.

### Native device isolation: local and CPU qualification

Newly prepared native profiles declare a frozen device-isolation policy. The
claim is sampled process observation and cooperative same-UID namespace leases,
not absolute exclusivity. Native admission, process namespace/PID interpretation,
SDK-activated visibility, original exit persistence and the G/H/host evidence
checks are separately exercised. The eager baseline, measurement-function AST,
suite, oracle, timing/aggregation and noise threshold remain unchanged. This
feature does not install any implicit model, token, credit or Trial budget.

A verified remote `not_started` receipt can settle its original launch fence
without an invented handle. The original dispatch boundary stays retained;
lost ACK recovery uses the original action and three exact byte artifacts.
Missing, invalid or uncommitted evidence stays unknown/fenced. A completed
original execution may be explicitly remeasured at the same source only after
its actual exit and nonqualifying isolation are independently validated. Old
measurements remain retained; new E and F keep the new original handle. A valid
existing completion is reused. These resource-admission outcomes are not
compiler failures and do not trigger an automatic dispatch loop.

Required G/H/host isolation checks bind four original artifacts, the source,
result, handle, action, policy and code pins, independently of model or adapter
claims. A no-baseline F-only correctness bootstrap remains available and cannot
produce a speedup or E3 receipt.

| Focused invocation | Observed result | Retained log |
| --- | --- | --- |
| Native supervisor, SDK entry, raw isolation and supervised-trial regression | 60 passed | `.validation/device-isolation/supervisor-entry-r3.log` |
| Adapter, preparation and original-handle recovery regression | 65 passed | `.validation/device-isolation/adapter-tests-04.log` |
| Core native admission and explicit same-source remeasurement | 11 passed | `.validation/native-admission/core-r2.log` |
| Isolation qualification/H/host plus existing noise qualification | 25 passed | `.validation/pipeline-isolation-r2.log` |
| Darwin process/lease module | 31 passed, one Linux-only skip | `.validation/device-isolation/process-helper-tests-r1.log` |
| Independent Linux CPU process/lease module | 31 passed, one Darwin-only skip | `.validation/device-isolation/linux-helper/cpu-helper-c113a24370124307a5bab935013a3a2b/qualification.json` |

The focused groups overlap; this table is not a unique-test sum. The Linux
qualification returned zero after one dispatch, with one read-only recovery,
original receipts retained and source hashes unchanged. It used only CPU
processes and standard-library process/lease primitives in a separate namespace;
it does not qualify real NPU occupancy or physical PID-view correspondence.
The latter remains a separate device probe.

The first three adapter logs and the initial metadata failure note preserve the
producer-identity fixture correction, new-policy assertion changes, fourth-pin
schema update and legacy rounded-handle compatibility failure. The original
six-module core invocation (`.validation/native-admission/core-regression-r1.log`)
has 90 passed, one null-ACK recovery error and one optional historical-fixture
skip out of 92 tests. The null-ACK error was fixed in the adapter and passed in
the subsequent complete pending-observation module within the 65-test run.
Neither failed run is retroactively labeled passing.

The first full Python run (`.validation/device-isolation/python-full-r1.log`)
ran **585 tests** in **209.115 seconds**, with **one failure and one skip**.
The failure is the original e19 compile-receipt replay's historical supervisor
pin compatibility; the unchanged assertion received unknown instead of the
known completed native failure. The skip is the actual Linux fdinfo check on
Darwin, separately passed in the Linux CPU qualification above. After the
compatibility correction, the second full Python invocation passed **585**
tests with **one Linux-only skip** (**586 total**) in **248.895 seconds**, exit
zero (`.validation/device-isolation/python-full-r2.log`). The complementary
Linux CPU run passed 31 tests and skipped its one Darwin-specific boundary.

The first full TUI run passed **15** groups; **12** could not run because the
root command omitted the existing `ui-test-deps` path from `PYTHONPATH` and
therefore could not import `pyte`. The failure is retained in
`.validation/device-isolation/tui-full-r1/`. The command's dependency path was
corrected without changing the product. The second full TUI invocation passed
**27/27**, with **zero failed, flaky or environmental groups**, timeout scale
**1.0** and exit zero. Root independently verified lengths and SHA256 digests
for all **145** referenced evidence files. The receipt is
`.validation/device-isolation/tui-full-r2/acceptance.json`, SHA256
`86dd5ce66ccdb1e03511b9041d7452dc52d24b4d259ac983c928e60c0cf743ec`.
This is a complete full-suite pass; the earlier failures remain recorded.

The complete passing invocations cover the main isolation version. They do
**not** cover the subsequent public PID-view initialization follow-up. That
feature is now implemented as `examples/scatter/prepare_npu_isolation.py` with
`initialize`, `prepare`, `dispatch` and `recover`, an explicitly selected
profile/transport/root/device, source-pinned receiver, one original dispatch
and original-identity recovery. Successful original proof produces
`initialized-profile.json`; unknown or invalid proof cannot produce a profile.
Campaign preparation rejects missing/invalid view proof before creating a
trial and provides the explicit initialization command. No host/device/operator
is hardcoded. README describes this flow and removes the obsolete fixed 20-call
wording without adding an implicit budget.

The initializer connected suite passed **65/65**, no skips
(`.validation/device-isolation/initializer-connected-summary.json`). Its
focused receiver and client runs passed **19/19** and **11/11**, respectively
(`product-receiver-acceptance.json` and `initializer-product-acceptance.md` in
that directory). These overlapping counts must not be summed. The client r1
fixture assertion failure remains retained; the corrected assertion binds the
returned view artifact and does not relax production checks. Preparation's
focused 51-test invocation passed 50 with one unavailable optional private
historical-receipt fixture (`preparation-product-summary.json`). The actual
suites use local CPU/CLI subprocesses and synthetic source-bound proof bytes;
they do not establish a physical NPU PID view or run an optimization candidate.

The final complete Python invocation ran **621 tests** in **256.055 seconds**:
**619 passed, one error and one skip**
(`.validation/device-isolation/python-final.log`). The sole error was the
`PreparedImportClosureTests` preparation fixture missing the newly required
synthetic PID-view proof. Production correctly rejected preparation before
creating a trial. The test fixture was corrected to supply that synthetic
proof (four lines added, one removed), with all original assertions and
production requirements retained. The complete pending-observation module then
passed **24/24** (`.validation/device-isolation/pending-observation-final.log`).
There is no remaining known failure in this tested scope. This is the original
621-test invocation followed by a targeted module pass, **not** a new full
621/621 invocation. The failed complete result remains retained, and its
Linux-only skip is separately covered on Linux.

The final focused TUI invocation passed **7/7**, with zero failed, flaky or
environmental groups at timeout scale **1.0**. It covers deployment, authority,
full JavaScript, interaction, advanced, Goal review and Goal plan. Full npm
passed **225** tests with one genuine case-sensitive-filesystem skip and zero
failures (**226 total**); authority passed **167/167**. The receipt is
`.validation/device-isolation/tui-final-focused/acceptance.json`, SHA256
`9ee81e547bfda62f3761f0f2e60b0dd70245208ed8f212b965f2456807c05c59`.
All **30** referenced artifacts matched their recorded lengths and SHA256
digests. This is a focused follow-up after the preceding main-isolation full
27/27 invocation, not another full 27/27 invocation for the initializer
follow-up. The complete-run failures and their subsequent focused corrections
remain separately recorded.

The next clean batch is planned as **min → max → matmul**, with both scatter
operators primary and Cube/matmul auxiliary. All candidates start blank, with
no total model-turn/token caps; usage remains recorded. Complete sessions and
all failed attempts, source versions, raw receipts and diagnostic reads will
be retained, alongside the existing full-session archives and unresolved
original handles. No new model or NPU optimization run is reported. These checks concern harness behavior and
establish no new successful optimization session, stable repetition, min/Cube
result or performance gain.
