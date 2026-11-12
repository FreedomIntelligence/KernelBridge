# Native Pi TUI and persistent sessions

The TUI and task-based SDK sessions use one local ApplicationHost, the pinned Pi 1.5.0 runtime, and the existing Executor Journal. A worker can exit while the product session and its accepted inputs remain durable. The implementation targets Linux and macOS; local acceptance was run on macOS. Linux and device qualification are pending; Windows requires WSL2.

The default entry is ordinary codegen: `kernelagent` opens the full native Pi `InteractiveMode` in the current project. A passive host and local session start with it; no model request, seed file, Goal, Trial, workspace-history initialization or evaluation starts before explicit input. An explicit initial prompt (`kernelagent "TASK"`) starts immediately. Missing model configuration leaves a usable setup/help screen. `--config`, `KERNELBRIDGE_CONFIG`, or `~/.kernelbridge/config.json` selects providers; credentials are read from the host environment or the current user's mode-0600 `~/.kernelbridge/credentials.json`; environment values take precedence. Only referenced variable names enter frozen configurations, and host restart reloads local credentials. `-C` selects the project. `resume` opens a searchable project-scoped picker; `resume --all` includes other projects. `resume ID_OR_TITLE`, `resume --last`, and `--continue` restore saved sessions without resubmitting their last prompt. `/name TITLE` supplies a persistent searchable title, including after the host exits.

Ordinary codegen tools act directly in the project. Conversation checkpoints do not snapshot or restore project files: `/new`, `/clear`, `/fork` and resume preserve the current working files, including untracked files, binary assets and installed dependencies. No whole-project size limit is imposed by the experiment snapshotter. The runtime is shared with the task mode below; execution admission, input queue, lease and receipts still use the same Journal.

Budget omission means no limit: `credits`, `policy.max_steps`, `max_model_turns`, `max_trials` and `max_forks` default to null. Only explicitly configured positive limits are enforced (`max_forks` may be zero). `--budget` sets a codegen credit limit. Usage remains accounted, but an unset limit is absent from the status bar. Context capacity, per-response output size and transport safety bounds are separate from execution budgets.

Goal scheduling is opt-in through `/goal OBJECTIVE`, `--goal OBJECTIVE`, explicit task `--goal-from-task`, or SDK `sessionOnly: false`. A codegen Goal can inspect progress with `get_goal` and report completion with `update_goal({status:"complete",summary:"actual checks"})` (`finish_goal` remains available); that report is not NPU acceptance evidence. While a Goal is active the model can also keep a short plan with `update_plan` (Codex's shape: the whole plan each call, `pending`/`in_progress`/`completed`). The host refuses more than one step in progress, empty or over-long steps and more than 12 steps. Each accepted call's receipt is the record. The TUI shows each update as a checklist and the footer shows `计划 n/m` and the step in progress. Automatic continuations carry the current plan. The fresh completion reviewer receives the final plan as the agent's own checklist, not as evidence. A turn that only updates the plan or reads the Goal is not progress. The tool is absent without a Goal and is not given to subagents. A later user prompt returns to ordinary codegen. Completed or cleared Goals stay ended after pause, resume or interrupt; use `/goal OBJECTIVE` to start new work. `/clear` starts a fresh conversation and preserves the historical ledger, without creating another Trial after Goal completion. Device acceptance still requires an explicit isolated task and evaluator/profile.

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

The session registry is `~/.kernelbridge/sessions`, or `KERNELBRIDGE_SESSIONS`. A running TUI discovers history from its authenticated host's registry, even while Pi uses a temporary isolated `HOME`. Both `resume` and `/resume` use the same native picker: search matches title, project path, branch and partial ID. Rows show relative activity, saved branch and journal size, grouped by project. The current session is included and marked `[当前]`; choosing it keeps the current conversation. Ctrl+A toggles all projects, Ctrl+B filters the current branch, and Ctrl+W includes worktrees sharing the Git common directory. Older records without a captured branch show it as unknown. Empty past startups are omitted from history and `--continue`; an explicit ID can still attach them. `/latest` only scrolls the current conversation and does not restore another session. Keep the registry path short enough for Unix sockets. The host directory must be owned by the current uid with mode 0700; token, socket and lock are private. Workers receive one-use tickets and a minimal environment. Model and device credential environment variables remain on the host; ordinary project shell tools run locally with the user file permissions. Configuration changes require the frozen profile's allowed model routes.

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
| `/goal`, `/goal pause`, `/goal resume`, `/goal clear` | View, pause, resume or clear the Goal. The status block stays live: elapsed time advances each second while the Goal runs, and model calls and tokens follow each refresh |
| `/help`, `/latest` | Show supported commands or return to the latest message |
| `/goal on-detach pause\|continue`, `/detach`, `/quit` | Set or apply detach behavior |
| `/compact` | Metered semantic compaction; preserve original JSONL and authoritative facts |
| `/context clean` | Receipt-aware projection; preserve original results and failures |
| `/context reset`, `/new` | New conversation in project codegen, including Goals; new Trial only in isolated task mode |
| `/fork`, `/fork ENTRY`, `/clone`, `/tree` | Fork current/historical conversation; isolated tasks also restore the recorded source; tree selection creates a fork |
| `/resume [ID_OR_TITLE]`, `/resume --all` | Project session picker or direct resume of the original host/history; no tool replay |
| `/resume PATH`, `/import PATH` in isolated task mode | Resume a retained original native session with its matching source checkpoint; no new Trial |
| `/subagents`, `/agents` | List available agent types and this session's runs; Enter opens a report, `s` stops a running child |
| `/trace` | Open this session's live trajectory (Lattice Trace) in the browser; see [trace.md](trace.md) |
| `/model ROUTE`, `/thinking LEVEL`, `/scoped-models` | Select only frozen profile routes; thinking requires a matching route |
| `/settings`, `/trust`, `/reload` | Inspect or reload the frozen resource set |
| `/export`, `/copy`, `/name`, `/session`, `/changelog`, `/hotkeys` | Native presentation; exports go under the run's `exports/` |
| `/login`, `/logout` | Show the host credential configuration path |
| `!command`, `!!command` | Project shell in codegen; guarded isolated bash in task mode; `!!` excludes its result from model context |

Sharing, bug upload, debug export, hidden and unknown commands are disabled. The source-verified Pi patch gates submission before ordinary, streaming and compaction branches. Patch or dependency mismatch refuses startup.

Goal command syntax follows [Codex's documented Goal interface](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex): `/goal` alone reads state; the text after `/goal` is the objective unless it is exactly `pause`, `resume`, or `clear`. For example, `/goal 1` sets the objective to `1`; `/goal pause the benchmark after collecting evidence` is an objective, not a pause command. KernelBridge additionally retains `/goal on-detach pause|continue`. Objectives and acceptance evidence still use the original Journal and budget. New objectives get separate Goal identities and usage counters. A blocked Goal enters `awaiting_user`; a new user message or `/goal resume` reactivates it. Paused Goals require explicit resume. A tool-free automatic continuation yields to the user, and queued user input has priority. Read-only `get_goal` calls alone do not count as progress. `--goal-token-budget N` requires `--goal`; the next call is denied when recorded usage reaches the limit, so the final in-flight call can exceed it. Parent, child and compaction model usage count together. Missing provider usage is shown as unknown, not zero-cost proof. Token/credit limits never imply completion.

This aligns the main Goal/session behaviors with Codex and Claude Code; it is not a claim of identical implementation. KernelBridge retains completed Goal evidence. An optional fresh completion evaluator is available for project Goals: add `"goal_review": {"enabled": true, "context_bytes": 131072}` to the model configuration. After each completed input, at a settled boundary, it evaluates the objective against retained model/tool receipts in a fresh, tool-free request. A working model's `finish_goal`/`update_goal complete` becomes a completion request and cannot skip this review. Verdicts are shown in the TUI and persisted as `met`, `continue`, or `blocked`. Reviews use the same model route, Journal, credit and Goal token limits; missing/invalid responses never complete a Goal, unknown effects remain fenced, and user pause/replacement wins over late replies. Review is disabled by default and does not run at ordinary idle startup. Like [Claude Code's documented evaluator](https://code.claude.com/docs/en/goal), it assesses presented evidence and does not execute commands independently. Explicit device acceptance still requires source-bound evaluator receipts.

The default `--tui-mode regular` keeps output in the terminal's own scrollback, like Claude Code; `--tui-mode fullscreen` restores Pi's alternate screen. The native editor completes only commands the host supports. Subagent plugin commands and other disabled entries do not appear in completion. Sending a prompt or command returns the viewport to the latest message; scrolling back while reading remains supported. `/latest` provides the same action without a terminal-specific shortcut. Command feedback appears above the editor. The compact footer shows the current model and directory in codegen, and explicit Goal/Trial details in experiment mode. Budget is shown only when set; full protocol receipts remain available through exports and SDK state. Automatic Goal prompts display a short continuation label while their complete original text stays in the Journal. A pause between model preparation and admission settles without calling the provider, and the same worker can resume.

Quit and detach wait for a settled operation; interrupt first if needed. Native display commands record their dispatch boundary; host commands such as compaction record success or failure after completion. `/new` starts a new conversation in ordinary codegen; in an isolated task it starts a fresh Trial. A project Goal does not implicitly create a Trial. Working files remain in place. Import accepts source-bound sessions from this application's retained archive.

## Subagents and fork suggestions

`delegate` is a model-visible tool. With the default `subagents.policy: "auto"` the parent delegates focused, independent investigations, reviews, test runs or scoped changes proactively; short lookups stay in the parent. User/project prohibitions take precedence; `policy: "explicit"` requires an explicit request, and `enabled: false` removes the tool.

| Agent | Tools | Available in |
| --- | --- | --- |
| `kernel-scout` | list/search/read files, skills and receipts | all modes |
| `kernel-reviewer` | the same, for a focused second review | all modes |
| `kernel-tester` | read tools plus `run_command` (tests, builds, diagnostics), foreground with parent writes excluded | project codegen |
| `kernel-worker` | read/write in an isolated Git worktree branch plus `run_command` | project codegen in a Git project with a committed HEAD |

Custom agents use the same Markdown format as Claude Code subagents: `~/.kernelbridge/agents/*.md` (user) and `<project>/.kernelbridge/agents/*.md` (project; overrides user by name). Frontmatter: `name`, `description`, optional `tools` (`read`, `test`, `write`, or a Claude Code tool list such as `Read, Grep, Bash`) and `model` (a configured route). The body is the agent's instructions. Isolated device tasks expose read-only agents only. Definitions are read when the host starts and become part of the frozen tool schema.

The parent supplies `agent`, a short `description` (shown in the TUI), a self-contained `task`, and optionally `mode` (`read_only` by default; `analysis` uses supplied text only), `max_steps` and `background`. Calls issued together in one reply run concurrently, up to `subagents.max_concurrent` (default 6); further calls queue. Results are recorded in reply order. With `background: true` the call returns immediately; the child keeps running across turns, and its result arrives as a `[子代理结果]` message that resumes the parent. `kernel-tester` requires foreground execution; use the isolated `kernel-worker` for background commands. `agents_status` reports progress, and `stop_agent` or `s` in `/agents` stops a running child, including its active command process group.

On exit, known effects settle before interruption is recorded. Unknown effects retain their reservations and block conflicting execution; exit never converts an unresolved command into a known failure. Recovery retains the original child IDs and sends a missing completion notification once. A completed worker keeps its Git branch and diff evidence while removing its temporary checkout, including when the child committed its own edits. An unresolved worker checkout is retained for recovery.

Like Claude Code and Codex, there are no default per-child step, read or time limits. `max_steps`, `tool_limit`, `timeout`, `max_per_turn` and `max_total` remain available as optional ceilings. When a child approaches the context bound, or reaches a configured ceiling, its last model call has no tools and returns the findings so far; that receipt is marked `partial` with a `completion_reason`. Shared credit and Goal token limits still apply, and concurrency can overshoot a Goal token limit by at most the calls already in flight.

`run_command` runs non-interactively in the project (tester) or in the worktree (worker), with a scrubbed environment and a private `HOME`; host model and device credentials are not inherited. Output keeps the last 64 KB. A worker's edits are committed to its own `kernelbridge/agent-*` branch; the result lists the diffstat and an apply command, and the parent decides whether to apply it. Worktrees without changes are removed. Nothing is merged or pushed automatically.

Each delegate row shows `⏺ agent(description)` with live progress (current tool, tool calls, model calls, tokens and elapsed time). While children run, the working row shows `等待 N 个子代理完成`. Ctrl+O shows a child's report. `/agents` lists the available agent types and this session's runs; Enter opens a report that continues updating while open. The selection stays attached to the original child when new entries arrive.

```json
{ "subagents": { "enabled": true, "policy": "auto", "max_concurrent": 6, "background": true } }
```

Child advice is not independent correctness or device evidence. Esc stops foreground children, preserves their in-flight receipts and permits another parent prompt after known cancellation settles. An unknown child effect remains fenced and must be reconciled; cancellation cannot disguise it as a known failure.

Implementation notes: a reply made only of delegate calls records a `delegation` batch plan, so its children do not form a source-order dependency chain in the Journal. Each delegation permit has its own ordered RPC lane, child model and tool calls run on host threads, and admission provenance is attributed to the delegate's own tool call. The children run in Pi through the community `pi-subagents@0.87.0` plugin; the host admits every child model and tool call.

When automatic forks are disabled, repeated tool failures or multiple compactions show a suggestion without changing the branch. Explicit task measurements can also produce a plateau suggestion. Project forks preserve files and spent usage; task forks require an exact retained source checkpoint. Resume preserves session identity; recovery may use a writable copy of the immutable native checkpoint, without changing the branch identity or replaying tools.

## Tool output and streaming

Tool calls render like other coding CLIs: a status header `⏺ Tool(main argument)` (grey while running, green on success, red on failure) and a one-line summary under `⎿`: lines read, lines written, `+a −b` with a short diff for edits, match/file counts for searches, and for shell commands the live tail while running, then duration and exit code. Test/build commands and failures keep the output tail; other commands keep the head. Errors show project-relative paths. Consecutive reads, searches and listings collapse into one `浏览` row. Ctrl+O shows every call and its full output; receipt-backed tools show readable fields instead of JSON.

Assistant text streams as it arrives; only a suffix that could still form a credential is held back. Reasoning deltas (`reasoning_content`) display as a collapsible thinking block while streaming and are not part of the settled reply.

`--tui-mode regular` renders inline, keeping output in the terminal's own scrollback like Claude Code; `--tui-mode fullscreen` uses Pi's alternate screen with in-app scrolling (`Ctrl+End` returns to the latest message). `KERNELBRIDGE_TUI_MODE` sets the same choice. `KERNELBRIDGE_UI_DETERMINISTIC=1` freezes the spinner and the UI's own clocks (elapsed time shows `—`) so screens can be compared byte for byte; durations reported by the host are still shown as reported.

Operator tools and no-Torch self iteration are documented in [operator-workflow.md](operator-workflow.md).

## Layout and theme

The interface follows the Claude Code / Codex layout. A welcome box shows the product, quick commands and the working directory. User messages are prefixed with `>` and assistant text with `⏺`; there are no background blocks, and one blank line separates transcript rows. Tool rows use status marks as described above. While the agent works, the row above the editor shows an animated `✻` mark and the current step: thinking, running a command, reading files, or waiting for N subagents. It also shows elapsed time, how many messages are queued behind the current turn, and `esc 中断`; on a narrow terminal these details are dropped from the right so the row stays on one line. An interrupted turn leaves a row saying so, with its tool receipts retained, rather than ending mid-thought.

While the editor is empty it shows a dim hint, like Codex's composer: `试试：修复失败的测试 · / 命令 · ! shell` in project mode and `输入约束或问题 · /goal 目标内容 持续执行` in task mode, shortened to fit the width. The first keystroke hides it. It is drawn by a host subclass of Pi's own `CustomEditor`, so keys, completion and submission are Pi's.

The footer has two lines. The first shows the model and state, plus Codex-style remaining context (`上下文剩余 X%`, or `剩余 X%` when narrow). The last shows the directory with branch and `/help`; when the path is too long its middle is elided so the directory's own name and the branch stay readable. Below that width the detach label is dropped before the branch is shortened. Running subagents and budgets appear only when relevant, and zero-valued reserved and pending counters are not shown. The bundled themes `kernelbridge-dark` and `kernelbridge-light` follow the terminal's appearance (`source/apps/agent/themes/`); `/settings` can select another installed theme.

`/help` prints one two-column table for both modes, listing the commands available in that mode followed by the keys. It fits a 24-row, 48-column terminal without clipping. Host feedback is marked `⏺` for notices and `✗` for errors, so product messages do not read as model output.

Host blocks are laid out at the terminal's real width. A labelled path such as `目录：` in `/goal` keeps its label and elides the path's middle. A narrower `/help` first tightens its command column, then puts a description under its command; lists of hints wrap between ` · ` items, never inside one. `validation/tui/layout.mjs` checks these at 48 columns and below.

## Goal policy and evidence

The profile's optional `application` object accepts `max_model_turns`, `max_forks`, `on_detach`, and `fork_policy` (`enabled`, `minimum_observations`, `cooldown_turns`). Existing `goal.max_trials` and `config.policy.max_steps` remain the total Trial and per-Trial limits. Compaction consumes the same model quota and credit ledger. LoopX recommendations are persisted with their evidence and never carry an independent spendable balance. The agent can call `context_transition` to request the same controlled boundary.

E0 is compilation, E1 full correctness, E2 a single complete timing observation, E3 a positive paired comparison above calibrated noise, and E4 an original platform receipt. E2 does not promote the best candidate. RSI application runs calibrate the baseline with repeated paired measurements using the same profile before optimization; calibration and later comparison consume evaluation credits. Changed evaluator/profile identities cannot reuse the previous calibration. Raw evidence, source, environment and task-version bindings remain in original receipts.

After an incumbent exists, another branch replaces it only with a qualifying pair against that incumbent under the same measurement profile. An improvement over an older parent remains recorded evidence. Changing task version clears the current best projection while preserving its historical receipts.

## Recovery and artifacts

The Journal is append-only and hash-chained. Each process verifies the whole chain once, then only what was appended since. It refuses a shorter file, a changed last record, or a replacement that does not extend the history it already verified. The whole chain is verified again every five minutes and on restart. Every transaction takes the exclusive lock. On macOS each append is flushed with `F_FULLFSYNC`, because `fsync` there leaves data in the drive cache; this costs about 3 ms per append.

`state/events.jsonl` and `state/pi-sessions/` are authoritative. `events.jsonl`, `loopx/state.json`, `manifest.json`, `result.json`, `acceptance.json`, and `state/host/projection.json` are rebuildable exports. The manifest records Pi/patch versions, prompt/tool/resource hashes and evaluator identity. Host diagnostics are in `logs/host.log` and background-worker diagnostics in `logs/worker.log`; production keystrokes are not recorded.

Restart reclaims the original host lock before clearing a stale socket. It never retries a model or device effect with an uncertain result. Original RSI submissions can be collected only through an explicitly configured read-only collector using the original action identity. Unknown submissions retain reservations and fence writers. Completed tool receipts missing from Pi are reattached to their original calls; missing assistant entries become explicitly recovered host messages, never fabricated assistant turns. Incomplete source/context transitions restore the retained parent before new execution.

Real Vector/Cube NPU validation, 20-turn device runs, Linux sandbox qualification and publication remain gated by their recorded acceptance results. Local deterministic tests cannot establish an NPU performance result.

See [the local acceptance runner](../validation/tui/README.md) for reproducible checks and [the recorded results](../validation/tui/ACCEPTANCE.md). Capability flags remain conservative until the full device and environment matrix is qualified.
