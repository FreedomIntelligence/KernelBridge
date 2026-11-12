# Live trajectory viewer

`/trace` in the TUI, or `kernelagent trace` from a shell, opens the session's trajectory in the browser. It uses the Lattice Trace viewer and updates while the session runs: model calls, tool calls, subagent lanes, context growth, compactions and file edits appear as the Journal records them.

```
/trace                                   # current session; opens the browser
kernelagent trace                        # this project's latest session
kernelagent trace ID_OR_TITLE [--all]    # a saved or running session
kernelagent trace --port 8765 --no-open  # fixed port, print the URL only
```

## How it works

```
session Journal (state/events.jsonl, append-only)
   │  tail, read-only (fs.watch + 1 s poll; a torn last line waits for its newline)
   ▼
journal-trace.ts   Journal rows → kernelbridge.trace/v1 (Lattice step model)
   │
server.ts          127.0.0.1:<port>/<token>/  page · trace.json · events (SSE)
   │
lattice-viewer.html + live-adapter.js   the viewer renders the live dataset
```

- **Authority.** The trace is derived from the Journal and never writes to it. In the TUI, the trusted CLI process hosts the server and the TUI worker only receives the URL over IPC. The worker gets no Journal access of its own.
- **Access.** The server binds 127.0.0.1 only and serves only under a random 128-bit path token. It rejects other `Host` headers, which stops DNS rebinding. Responses are `no-store`. The `/trace` server lives as long as the interactive session; `kernelagent trace` lives until Ctrl-C.
- **Live updates.** Each change re-projects the Journal; projection takes about 4 ms for 1,200 rows. The full trace is pushed as an SSE `trace` event. The page applies at most two updates per second. It follows the newest step unless you selected an older one, and it keeps zoom, lane visibility and open panels.

## Mapping

| Journal | Trace step |
| --- | --- |
| `__propose__` outcome (text, tool calls, `usage`) | model step: `tokIn` = `prompt_tokens`, `tokOut` = `completion_tokens`, provider cache hits (`prompt_cache_hit_tokens` / `cached_tokens`) |
| tool action + `settled` (`elapsed_ms`, result, `isError`, gate denial) | tool call: title, arguments, output (first 4,000 characters), status |
| `delegate` + child task `…/leaf` | spawn step, one lane per child, return step with the child's summary |
| `…/compact/…` proposal | compaction with before/after tokens and the summary |
| `transition_prepared` (fork / reset) | `/fork` or `/new` marker |
| `input_accepted` | user input, command, Goal continuation or subagent notification |
| successful `write` / `edit` | file version used by the 溯源 (blame) view; contents rebuilt from these calls only |

Executor rows have no clock. Each step's start comes from the nearest application row, and its duration from `elapsed_ms`. Step IDs come from Journal sequence numbers, so they stay the same while the session grows. Tool-output sizes are estimated from text and then scaled between consecutive model calls, so the rebuilt context matches the provider's `prompt_tokens`.

## Viewer

`source/apps/agent/src/trace/lattice-viewer.html` is a vendored, unmodified copy of Lattice Trace v15 (`Agent轨迹Demo-v15-20261104.html`, sha256 `bc5e28f7…742e`). At serve time the server:

1. embeds the initial trace as JSON;
2. injects `live-adapter.js` just before the viewer boots;
3. applies a short table of exact-match replacements: live base context, the 实时会话 label, the real harness configuration, and no simulated fork button.

Each replacement anchor must occur exactly once. A changed viewer therefore fails with an explicit error instead of rendering demo data. To update the viewer, replace the file and run `node validation/tui/trace-projection.mjs`.

The live page shows one session; the demo studies are hidden. Views that need data KernelBridge does not record are empty: branch comparison, because live sessions have one branch, and workspace snapshots, because project mode does not snapshot.

## Validation

- `node validation/tui/trace-projection.mjs`: projection of a synthetic Journal, covering children, edits, compaction, fork, calibration, stable IDs and torn tails. Also viewer assembly and transforms, and the server's token, Host check and SSE growth.
- `validation/tui/trace_scenario.py --launcher kernelagent --output DIR`: a real TUI session that runs a read and two subagents, then `/trace`. It checks the served trace, SSE growth on a new turn, and a headless Chrome render with no page errors (when Playwright is installed). It also checks that the server stops on `/quit` and that standalone `kernelagent trace` reads the saved session.
