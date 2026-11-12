# TUI polish plan

Status: in progress. Baseline is `6dc9a79` (welcome box, `>`/`⏺` gutters, working row, two-line footer, bundled themes, `/trace`).

This plan continues [tui-subagent-redesign.md](tui-subagent-redesign.md). That document covered behavior (SSE, tool output, subagents, concurrency). This one covers the remaining presentation and interaction gaps against Claude Code and Codex, each one observed in a rendered screen of our own TUI rather than assumed.

## Method

Every item below was found by rendering real screens from the PTY scenarios (`validation/tui/*.py`, saved as `*-screen.txt` and transcripts) and comparing against the two reference CLIs. Each item states the defect, the intended behavior and how it is checked. An item is done when a check in `validation/tui/` fails without the change and passes with it.

Rules carried over from the redesign:

- Presentation never invents facts. Everything rendered comes from host state, the Journal, or Pi's own entries.
- Chinese for user-visible text, English for model-facing text.
- No new per-child limits, no policy knobs in the UI (Claude Code / Codex parity).
- Changes to the Pi patch must keep `patches/*.json` digests and `install.py --verify-only` green.

## Observed defects

| # | Where | Observed | Intended |
| --- | --- | --- | --- |
| D1 | Transcript spacing | Two blank lines before and after every `>` user row (`submitted-screen.txt`) | One blank line, as Claude Code; no double gaps around consecutive rows |
| D2 | `/help` in task mode | Dense one-line-per-topic list (`COMMAND_HELP`), no `/agents` or `/trace`, wraps mid-word at 48 columns (`narrow-help-screen.txt`) | One two-column table for both modes, wrapped to the terminal width |
| D3 | `/goal` status | Bare `key：value` lines; the absolute path wraps onto its own line leaving a stray `：` (`goal-screen.txt`) | Aligned block, `~` path, elided middle, no wrap at 48 columns |
| D4 | Feedback lines | `不可用的命令：…` renders unmarked, same weight as assistant text (`rejected-command-screen.txt`) | A distinct marker and color for host feedback, errors separated from notices |
| D5 | Empty editor | Two bare rules with an empty line between (all screens) | A placeholder hint inside the editor, like Codex's composer |
| D6 | Tool rows without a main argument | `⏺ evaluate_candidate` with no parentheses | Always `Tool(主参数)`; pick the most informative field, or the receipt kind |
| D7 | Footer, task mode | `预算 17/200 · 预留 0 · 待处理 0` shows zeros | Hide zero reserved/pending; keep budget only when set |
| D8 | Footer path | `~/Desktop/…/workspace/91c26acccc0479…` truncates the tail, hiding the directory name | Elide the middle, keep the first and last segment |
| D9 | Queued input | No indication that text typed during a run is queued | Show queued count above the editor, as Claude Code's `⏵⏵` |
| D10 | Interrupt | Esc shows no transcript record | One `⏺ 已中断` row, so scrollback shows where it stopped |
| D11 | Plan / todo | No plan surface at all | Decide: adopt a host-governed plan tool (Codex `update_plan`, Claude Code `TodoWrite`) or document the deliberate absence |
| D12 | Narrow terminals | Help and goal blocks assume width; no check below 60 columns | All host-rendered blocks wrap at the real width; a 48-column check in the scenario |

## Phases and TODO

### Phase 1 — transcript and host blocks (D1, D2, D3, D4, D6)

- [x] D1 Collapse the blank line that the user-row gutter adds; keep exactly one separating line between transcript rows.
- [x] D2 Replace `COMMAND_HELP` with the two-column table used in project mode, filtered by mode, including `/agents` and `/trace`; wrap to width.
- [x] D3 Render `/goal` status as an aligned block with `shortPath` and middle elision.
- [x] D4 Give host feedback a marker (`⏺` notice, `✗` error) and the theme's notice/error color; keep the text itself unchanged.
- [x] D6 Extend `toolTitle` so every tool row has a main argument; fall back to the receipt's kind for business tools.
- [x] Checks: `validation/tui/subagent-presentation.mjs` (8 checks) and `interaction.mjs` cover elision, help columns and tool titles; `interaction_scenario.py` asserts the 48-column screen.

### Phase 2 — editor and live state (D5, D9, D10)

D7 and D8 shipped with Phase 1: they live in the same footer renderer.

- [x] D5 Placeholder in the empty editor, cleared on first keystroke: `试试：修复失败的测试 · / 命令 · ! shell` (project) or `输入约束或问题 · /goal 目标内容 持续执行` (task), the widest variant that fits. `@ 文件` was left out: file completion depends on Pi's managed `fd`, which may be absent, so the hint would promise something that might not work. Implemented host-side as a `CustomEditor` subclass installed with `setEditorComponent`; no Pi patch change. Checks: `layout.mjs` (hint at 48 and 28 columns, hidden after a keystroke, back when empty) and `interaction_scenario.py` (`editor_hint`: visible after startup and at 48 columns, gone once `/go` is typed).
- [x] D7 Footer: drop zero-valued reserved and pending; budget only when configured.
- [x] D8 Footer path: elide the middle, keep the last segment and the branch.
- [x] D9 Queued input is counted from host receipts and shown in the working row (`执行中… (12s · 2 条排队 · esc 中断)`).
- [x] D10 An interrupted turn leaves a visible row instead of ending mid-thought.
- [x] Checks: `subagent-presentation.mjs` covers the queued count and its absence when nothing is queued; `codegen_scenario.py` (`interrupt_then_continue`) covers the interrupt row.

Found while fixing Phase 1, already handled:

- [x] The feedback widget clipped `/help` at half the terminal height. It now uses the height the editor and footer leave free, and the help table fits 24 rows at 48 columns.

### Phase 3 — plan surface (D11) and width (D12)

- [x] D11 Adopted for Goal mode only, after surveying Codex, Claude Code, Gemini CLI, Cline, Amp and OpenHands ([industry-alignment.md](industry-alignment.md) §1): `update_plan` in Codex's shape, validated by the host, its receipt is the record, checklist cells and a `计划 n/m` footer, carried into continuations and the completion review, not counted as progress. Checks: `validation/tui/test_goal_plan.py` (9), `plan-presentation.mjs` (4), `goal_plan_scenario.py` (5).
- [x] D12 Every host-rendered block fits the terminal width. Found by rendering each block at 48/40/32/24/16 columns: `/goal` wrapped a long path away from `目录：` even at 48 columns (it was elided to a fixed 52); narrower `/help` wrapped descriptions to column 0 and split key hints mid-item; the footer cut the branch (`(mai…`) at 32 columns; the working row (`等待 1 个子代理完成… (1m05s · 2 条排队 · esc 中断)`, 54 columns) wrapped to two lines at 48. Now the feedback block elides labelled paths and reflows tables at render width, the footer drops `上下文` / the detach label before shortening the branch, and the working row drops details from the right. The welcome box, `/agents` panel and tool rows already fit. Checks: `validation/tui/layout.mjs` (4 width checks, each failing on the previous code) and `interaction_scenario.py` (`narrow_goal_path` at 48 columns).

### Phase 4 — acceptance

- [x] Run the full PTY suite (`pty`, `interaction`, `advanced`, `cancel`, `edges`, `resume`, `goalreview`, `codegen`, `trace`) on an idle machine. `run.py` ran all 27 checks on `5bb40df` at 0.8–2.0 load per CPU: 27 passed, none flaky.
- [x] Refresh `PIN_MANIFEST.json`, `install.py --verify-only`.
- [x] Record results in `validation/tui/ACCEPTANCE.md` and update `docs/tui.md`.

## Known test fragility, and the product finding behind it

Across three suite runs, `pty_scenario.py` and `advanced_scenario.py` failed only by timeout — never by assertion — and at a different step each time (a `Client.call('status')` socket timeout, a screen condition during a 2500-line output, a 90-second wait during the pipeline compile). Throughout, unrelated system processes held this workstation's load average between 18 and 70: a WebKit renderer pegged near 90% of a core for four days, `StorageManagementService` for over an hour, `WindowServer` at 43%, with the data volume 100% full.

Run alone on the same code, `pty_scenario.py` passes. These timeouts are environmental and are recorded as such, not as passes.

The investigation did surface a real characteristic worth fixing separately. `Journal._transaction` (`state/journal.py`) takes an exclusive `flock` and re-reads and re-verifies the whole hash chain on every call, including the read-only `status` path, which runs two such transactions. So `status` is O(journal size) and serializes against every write. The TUI's one-second working row uses cached state rather than polling, so the exposure is bounded to turn boundaries — but a long session's boundaries get slower as its journal grows, and under load this is what the tests hit.

- [x] Done differently from this note, after surveying event stores and transparency logs ([industry-alignment.md](industry-alignment.md) §2): each process keeps a verified prefix (identity, offset, last digest) and verifies only appended records; `status` is one shared read instead of four. Measured 856 ms → 116 ms per snapshot under load. Checks: `validation/tui/test_journal_read_path.py` (11).
