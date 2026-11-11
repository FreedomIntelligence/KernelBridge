# Native Pi TUI and persistent sessions

The TUI and task-based SDK sessions use one local ApplicationHost, the pinned Pi 1.5.0 runtime, and the existing Executor Journal. A worker can exit while the product session and its accepted inputs remain durable. The implementation targets Linux and macOS; local acceptance was run on macOS. Linux and device qualification are pending; Windows requires WSL2.

The default entry is ordinary codegen: `kernelagent` opens the full native Pi `InteractiveMode` in the current project. A passive host and local session start with it; no model request, seed file, Goal, Trial, workspace-history initialization or evaluation starts before explicit input. An explicit initial prompt (`kernelagent "TASK"`) starts immediately. Missing model configuration leaves a usable setup/help screen. `--config`, `KERNELBRIDGE_CONFIG`, or `~/.kernelbridge/config.json` selects providers; credentials are read from the host environment or the current user's mode-0600 `~/.kernelbridge/credentials.json`; environment values take precedence. Only referenced variable names enter frozen configurations, and host restart reloads local credentials. `-C` selects the project. `resume` opens a searchable project-scoped picker; `resume --all` includes other projects. `resume ID_OR_TITLE`, `resume --last`, and `--continue` restore saved sessions without resubmitting their last prompt. `/name TITLE` supplies a persistent searchable title, including after the host exits.

Ordinary codegen tools act directly in the project. Conversation checkpoints do not snapshot or restore project files: `/new`, `/clear`, `/fork` and resume preserve the current working files, including untracked files, binary assets and installed dependencies. No whole-project size limit is imposed by the experiment snapshotter. The runtime is shared with the task mode below; execution admission, input queue, lease and receipts still use the same Journal.

Budget omission means no limit: `credits`, `policy.max_steps`, `max_model_turns`, `max_trials` and `max_forks` default to null. Only explicitly configured positive limits are enforced (`max_forks` may be zero). `--budget` sets a codegen credit limit. Usage remains accounted, but an unset limit is absent from the status bar. Context capacity, per-response output size and transport safety bounds are separate from execution budgets.

Goal scheduling is opt-in through `/goal OBJECTIVE`, `--goal OBJECTIVE`, explicit task `--goal-from-task`, or SDK `sessionOnly: false`. A codegen Goal can inspect progress with `get_goal` and report completion with `update_goal({status:"complete",summary:"actual checks"})` (`finish_goal` remains available); that report is not NPU acceptance evidence. A later user prompt returns to ordinary codegen. Completed or cleared Goals stay ended after pause, resume or interrupt; use `/goal OBJECTIVE` to start new work. `/clear` starts a fresh conversation and preserves the historical ledger, without creating another Trial after Goal completion. Device acceptance still requires an explicit isolated task and evaluator/profile.

Install using the root README. Select a task/profile only when running an experiment:

```sh
./kernelagent tui --task /absolute/tasks/vector-add/task.json --profile /absolute/npu.json --output /absolute/runs/vector
./kernelagent tui --task /absolute/tasks/cube-matmul/task.json --profile /absolute/npu.json --output /absolute/runs/cube
./kernelagent sessions
./kernelagent session status APPLICATION_ID
./kernelagent tui --attach APPLICATION_ID
./kernelagent tui --attach APPLICATION_ID --restart
```

`--session-only` is the default; `--goal-from-task` explicitly enables automatic Goal scheduling. `--on-detach pause` is the default. Choose `continue` explicitly to permit an active Goal to continue in a headless worker after the terminal exits. `/detach` selects continue for that exit. Esc enters `awaiting_user`; only new user input or an explicit resume releases it. `/goal pause` requires an explicit resume. A second TUI cannot replace a live TUI. Attach requests a safe handoff from a headless worker and waits for its original effect to settle.

The session registry is `~/.kernelbridge/sessions`, or `KERNELBRIDGE_SESSIONS`. Keep this path short enough for Unix sockets. The host directory must be owned by the current uid with mode 0700; token, socket and lock are private. Workers receive one-use tickets and a minimal environment. Model and device credential environment variables remain on the host; ordinary project shell tools run locally with the user file permissions. Configuration changes require the frozen profile's allowed model routes.

## SDK

```js
import { KernelCat, ApplicationSession } from '/absolute/KernelBridge/source/apps/agent/src/sdk/index.ts';

const kernel = new KernelCat({ config: '/absolute/run/config.json',
  python: '/absolute/runtime/venv/bin/python', expectedPythonModules: '/absolute/runtime/expected.json',
  timeoutMs: 300000, maxFrameBytes: 16777216 });
const session = await kernel.createSession({ task: '/absolute/run/task.json', output: '/absolute/run' });
await session.prompt('Preserve the declared tolerances.', { inputId: 'constraint-1' });
await session.observe();
session.subscribe(event => { /* token deltas are presentation; receipts establish effects */ });
await session.run();
session.dispose();
const attached = await ApplicationSession.attach(session.applicationSessionId);
console.log(await attached.state());
attached.dispose();
```

`prompt`, `steer`, `followUp`, and `command` accept inputs even while no model call is active. `run()` starts a headless worker; `dispose()` only detaches this SDK client. Use `pause()`, `resume()`, `interrupt()`, `clearGoal()`, `setGoal()`, and `onDetach()` for control. Reuse the same input ID and exact content after an uncertain acknowledgement. A changed body with the same ID is rejected. `events(cursor)` replays paginated Journal facts before live updates. Reconnect after a slow-consumer disconnect using the last durable cursor.

The original `createSession({manifest})`, `runTask`, and external LoopX APIs remain available as compatibility interfaces. Their finite managed invocation semantics and qualification flags are preserved. The new task-based overload is the persistent product interface. `examples/cann/application.mjs` prepares a CANN profile and runs that same interface.

`setGoal(objective, { acceptance: { evidence_tier: 'E3', target: 1.5 } })` versions the objective and acceptance criteria together. Use `target: null` to clear a target. Existing submitted evaluations keep their original task version; they cannot complete the revised Goal. Evaluator files and measurement conditions stay frozen for this application.

## Commands

| Command | Behavior |
| --- | --- |
| `/goal TEXT` | Create or update the persistent objective and start continuation |
| `/goal`, `/goal pause`, `/goal resume`, `/goal clear` | View, pause, resume or clear the Goal |
| `/help`, `/latest` | Show supported commands or return to the latest message |
| `/goal on-detach pause\|continue`, `/detach`, `/quit` | Set or apply detach behavior |
| `/compact` | Metered semantic compaction; preserve original JSONL and authoritative facts |
| `/context clean` | Receipt-aware projection; preserve original results and failures |
| `/context reset`, `/new` | New conversation in project codegen, including Goals; new Trial only in isolated task mode |
| `/fork`, `/fork ENTRY`, `/clone`, `/tree` | Fork current/historical conversation; isolated tasks also restore the recorded source; tree selection creates a fork |
| `/resume [ID_OR_TITLE]`, `/resume --all` | Project session picker or direct resume of the original host/history; no tool replay |
| `/resume PATH`, `/import PATH` in isolated task mode | Resume a retained original native session with its matching source checkpoint; no new Trial |
| `/subagents`, `/agents` | Show configured limits, child results and explicit trigger guidance |
| `/model ROUTE`, `/thinking LEVEL`, `/scoped-models` | Select only frozen profile routes; thinking requires a matching route |
| `/settings`, `/trust`, `/reload` | Inspect or reload the frozen resource set |
| `/export`, `/copy`, `/name`, `/session`, `/changelog`, `/hotkeys` | Native presentation; exports go under the run's `exports/` |
| `/login`, `/logout` | Show the host credential configuration path |
| `!command`, `!!command` | Project shell in codegen; guarded isolated bash in task mode; `!!` excludes its result from model context |

Sharing, bug upload, debug export, hidden and unknown commands are disabled. The source-verified Pi patch gates submission before ordinary, streaming and compaction branches. Patch or dependency mismatch refuses startup.

Goal command syntax follows [Codex's documented Goal interface](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex): `/goal` alone reads state; the text after `/goal` is the objective unless it is exactly `pause`, `resume`, or `clear`. For example, `/goal 1` sets the objective to `1`; `/goal pause the benchmark after collecting evidence` is an objective, not a pause command. KernelBridge additionally retains `/goal on-detach pause|continue`. Objectives and acceptance evidence still use the original Journal and budget. New objectives get separate Goal identities and usage counters. A blocked Goal enters `awaiting_user`; a new user message or `/goal resume` reactivates it. Paused Goals require explicit resume. A tool-free automatic continuation yields to the user, and queued user input has priority. Read-only `get_goal` calls alone do not count as progress. `--goal-token-budget N` requires `--goal`; the next call is denied when recorded usage reaches the limit, so the final in-flight call can exceed it. Parent, child and compaction model usage count together. Missing provider usage is shown as unknown, not zero-cost proof. Token/credit limits never imply completion.

This aligns the main Goal/session behaviors with Codex and Claude Code; it is not a claim of identical implementation. KernelBridge retains completed Goal evidence. An optional fresh completion evaluator is available for project Goals: add `"goal_review": {"enabled": true, "context_bytes": 131072}` to the model configuration. After each completed input, at a settled boundary, it evaluates the objective against retained model/tool receipts in a fresh, tool-free request. A working model's `finish_goal`/`update_goal complete` becomes a completion request and cannot skip this review. Verdicts are shown in the TUI and persisted as `met`, `continue`, or `blocked`. Reviews use the same model route, Journal, credit and Goal token limits; missing/invalid responses never complete a Goal, unknown effects remain fenced, and user pause/replacement wins over late replies. Review is disabled by default and does not run at ordinary idle startup. Like [Claude Code's documented evaluator](https://code.claude.com/docs/en/goal), it assesses presented evidence and does not execute commands independently. Explicit device acceptance still requires source-bound evaluator receipts.

The native editor completes only commands the host supports. Subagent plugin commands and other disabled entries do not appear in completion. Sending a prompt or command returns the viewport to the latest message; scrolling back while reading remains supported. `/latest` provides the same action without a terminal-specific shortcut. Command feedback appears above the editor. The compact footer shows the current model and directory in codegen, and explicit Goal/Trial details in experiment mode. Budget is shown only when set; full protocol receipts remain available through exports and SDK state. Automatic Goal prompts display a short continuation label while their complete original text stays in the Journal. A pause between model preparation and admission settles without calling the provider, and the same worker can resume.

Quit and detach wait for a settled operation; interrupt first if needed. Native display commands record their dispatch boundary; host commands such as compaction record success or failure after completion. `/new` starts a new conversation in ordinary codegen; in an isolated task it starts a fresh Trial. A project Goal does not implicitly create a Trial. Working files remain in place. Import accepts source-bound sessions from this application's retained archive.

## Subagents and fork suggestions

The implementation uses the **community `pi-subagents@0.87.0` plugin**, through its public delegation events, required-child extension and capability ceiling. It is not an official built-in Pi subagent component. Pi supplies child session orchestration; the host admits each child model/tool call and checks its original receipts.

Ask explicitly, for example: `Use two subagents: kernel-scout to inspect the indexing contract, and kernel-reviewer to review the candidate against the retained checks.` The parent can call `delegate` with `agent`, `task`, `mode: "analysis" | "read_only"`, and optional `max_steps`. A normal prompt does not itself request delegation. Descriptions and project instructions govern when the model chooses delegation; they are not a natural-language authorization classifier.

```json
{
  "subagents": {
    "enabled": true,
    "max_per_turn": 4,
    "max_total": null,
    "max_steps": 6,
    "tool_limit": 16,
    "timeout": 120
  },
  "fork_policy": { "enabled": false, "minimum_observations": 3, "cooldown_turns": 3 }
}
```

`max_per_turn` counts delegate calls in one model reply; `max_total` counts attempted delegations across the session, including rejected attempts. `null` leaves that total unlimited. Children currently run sequentially in the foreground. Analysis mode has one model step and no tools; read-only mode can inspect permitted files, skills and receipts. Neither mode can write, invoke shell/device tools or spawn nested children. Counts, steps, tools and timeout are validated on the host; the shared credit/Goal token limits also apply. Child advice is not independent correctness or device evidence. A failed child can settle without losing the parent. Esc stops the child, preserves its in-flight receipts, and permits another parent prompt after known cancellation settles. An unknown child effect remains fenced and must be reconciled; cancellation cannot disguise it as a known failure.

When automatic forks are disabled, repeated tool failures or multiple compactions show a suggestion without changing the branch. Explicit task measurements can also produce a plateau suggestion. Project forks preserve files and spent usage; task forks require an exact retained source checkpoint. Resume preserves session identity; recovery may use a writable copy of the immutable native checkpoint, without changing the branch identity or replaying tools.

Operator tools and no-Torch self iteration are documented in [operator-workflow.md](operator-workflow.md).

## Goal policy and evidence

The profile's optional `application` object accepts `max_model_turns`, `max_forks`, `on_detach`, and `fork_policy` (`enabled`, `minimum_observations`, `cooldown_turns`). Existing `goal.max_trials` and `config.policy.max_steps` remain the total Trial and per-Trial limits. Compaction consumes the same model quota and credit ledger. LoopX recommendations are persisted with their evidence and never carry an independent spendable balance. The agent can call `context_transition` to request the same controlled boundary.

E0 is compilation, E1 full correctness, E2 a single complete timing observation, E3 a positive paired comparison above calibrated noise, and E4 an original platform receipt. E2 does not promote the best candidate. RSI application runs calibrate the baseline with repeated paired measurements using the same profile before optimization; calibration and later comparison consume evaluation credits. Changed evaluator/profile identities cannot reuse the previous calibration. Raw evidence, source, environment and task-version bindings remain in original receipts.

After an incumbent exists, another branch replaces it only with a qualifying pair against that incumbent under the same measurement profile. An improvement over an older parent remains recorded evidence. Changing task version clears the current best projection while preserving its historical receipts.

## Recovery and artifacts

`state/events.jsonl` and `state/pi-sessions/` are authoritative. `events.jsonl`, `loopx/state.json`, `manifest.json`, `result.json`, `acceptance.json`, and `state/host/projection.json` are rebuildable exports. The manifest records Pi/patch versions, prompt/tool/resource hashes and evaluator identity. Host diagnostics are in `logs/host.log` and background-worker diagnostics in `logs/worker.log`; production keystrokes are not recorded.

Restart reclaims the original host lock before clearing a stale socket. It never retries a model or device effect with an uncertain result. Original RSI submissions can be collected only through an explicitly configured read-only collector using the original action identity. Unknown submissions retain reservations and fence writers. Completed tool receipts missing from Pi are reattached to their original calls; missing assistant entries become explicitly recovered host messages, never fabricated assistant turns. Incomplete source/context transitions restore the retained parent before new execution.

Real Vector/Cube NPU validation, 20-turn device runs, Linux sandbox qualification and publication remain gated by their recorded acceptance results. Local deterministic tests cannot establish an NPU performance result.

See [the local acceptance runner](../validation/tui/README.md) for reproducible checks and [the recorded results](../validation/tui/ACCEPTANCE.md). Capability flags remain conservative until the full device and environment matrix is qualified.
