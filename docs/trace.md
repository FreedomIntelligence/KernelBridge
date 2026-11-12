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

The viewer, page assembly, step helpers and HTTP server live in `source/packages/pi-lattice-trace/` and are shared with the Pi plugin below. `source/apps/agent/src/trace/` keeps only the Journal projection and the Journal tail.

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
| `application/goal`, accepted `update_plan`, `goal_review` (also inside atomic rows) | `progress`: the latest Goal, the plan history, completion reviews, model calls and tokens since the Goal started |

Executor rows have no clock. Each step's start comes from the nearest application row, and its duration from `elapsed_ms`. Step IDs come from Journal sequence numbers, so they stay the same while the session grows. Tool-output sizes are estimated from text and then scaled between consecutive model calls, so the rebuilt context matches the provider's `prompt_tokens`.

## Viewer

`source/packages/pi-lattice-trace/viewer/lattice-viewer.html` is a vendored, unmodified copy of Lattice Trace v15 (`Agent轨迹Demo-v15-20261104.html`, sha256 `bc5e28f7…742e`). At serve time the server:

1. embeds the initial trace as JSON;
2. injects `live-adapter.js` just before the viewer boots;
3. applies a short table of exact-match replacements: live base context, the 实时会话 label, the real harness configuration, and no simulated fork button.

Host wording in the adapter (harness name, data source, workspace and file notes) comes from the trace's optional `labels`; without it the KernelBridge text applies.

Each replacement anchor must occur exactly once. A changed viewer therefore fails with an explicit error instead of rendering demo data. To update the viewer, replace the file and run `node validation/tui/trace-projection.mjs`.

**进展 › 目标与计划** shows the session's Goal (objective, status, elapsed time that advances while the Goal runs, model calls and tokens against its limits), the current `update_plan` checklist with every accepted revision (each links to its step), the completion reviews, a progress curve (plan completion, model calls and file edits over time) and the latest activity. Refused plans are not part of the history. A session without a Goal shows the curve and the activity only; a Pi session never has a Goal.

The live page shows one session; the demo studies are hidden. Views that need data KernelBridge does not record are empty: branch comparison, because live sessions have one branch, and workspace snapshots, because project mode does not snapshot.

## Validation

- `node validation/tui/trace-projection.mjs`: projection of a synthetic Journal, covering children, edits, compaction, fork, calibration, stable IDs and torn tails. Also viewer assembly and transforms, and the server's token, Host check and SSE growth.
- `validation/tui/trace_scenario.py --launcher kernelagent --output DIR`: a real TUI session that runs a read and two subagents, then `/trace`. It checks the served trace, SSE growth on a new turn, and a headless Chrome render with no page errors (when Playwright is installed). It also checks that the server stops on `/quit` and that standalone `kernelagent trace` reads the saved session.

## Pi plugin

The same viewer also ships as a Pi package, `source/packages/pi-lattice-trace`, for plain Pi without KernelBridge. It reads the current session from `ctx.sessionManager` inside the Pi process, adds executing tools and a streaming reply from Pi's events, and serves the result with the shared server. See its [README](../source/packages/pi-lattice-trace/README.md).

```sh
pi install ./source/packages/pi-lattice-trace
/trace                                   # in Pi
```

KernelBridge loads Pi with `noExtensions` and an explicit factory list, so an installed copy of the plugin never loads inside `kernelagent` and does not compete with its `/trace`.

- `node validation/tui/pi-lattice-trace.mjs`: Pi session projection (token counts, parallel tools, errors, compaction, markers, `!!` commands, file history, running and streaming steps, stable ids), the Pi page and source switching on the shared server.
- `python3 validation/tui/pi_plugin_trace_scenario.py --output DIR`: real Pi 1.5.0 in RPC mode with an isolated `PI_CODING_AGENT_DIR` and a scripted provider. It checks the served trace, a running tool, SSE growth, a headless Chrome render with Pi labels, `/new` on the same URL, and the server stopping when Pi exits.

## Pi subagents

In the Pi plugin each child of a pi-subagents `subagent` call becomes a lane: a spawn on the parent, the child's own steps (from the result's messages, or its session file), and a return when the result arrives. A child that has not returned yet is drawn without the return edge.

## Branches

The viewer compares branches that share one fork point.

- Pi: other branches of the session tree (left by `/tree`) become lanes B, C and D. They fork at the latest entry on the current path that has other children, and each follows its newest descendant.
- KernelBridge: a `/fork` from a historical entry shows the turns it left behind as branch B, forked where the selected entry was checkpointed. A fork from the latest entry leaves nothing behind and stays a marker.

The vendored viewer orders the fork prefix by comparing step ids as strings, which only its demo ids satisfy. The live page replaces those comparisons with timeline order (`kbAtOrBefore`). It also skips inputs and markers when it pairs steps for the divergence summary, and stops a one-lane branch history from dividing by zero. Each replacement is an exact match that must occur once.

## OpenTelemetry export

`kernelagent trace ID --otlp`, `/trace --otlp` in Pi, or `lattice-trace FILE --otlp` sends a session once as OTLP/HTTP JSON to `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, or to `OTEL_EXPORTER_OTLP_ENDPOINT` + `/v1/traces`; `OTEL_EXPORTER_OTLP_HEADERS` is honoured. Nothing is sent unless you run one of these commands.

| Trace | Span |
| --- | --- |
| main lane, subagent lane | `invoke_agent main`, `invoke_agent NAME` (a subagent under its delegate call) |
| model step | `chat MODEL`: `gen_ai.usage.input_tokens` (including cached tokens), `output_tokens`, `cache_read.input_tokens`, `cache_write.input_tokens`, `reasoning.output_tokens`, `finish_reasons` |
| tool step | `execute_tool TOOL` under the model call that made it, with `gen_ai.tool.call.id` where the source records it |
| compaction | `kernelbridge.compaction` with `tokens_before` / `tokens_after`; the next model span sets `gen_ai.conversation.compacted` |
| input, fork, checkpoint | events on the lane span |

Spans also carry OpenInference kinds (`AGENT`, `LLM`, `TOOL`), so Phoenix and Langfuse group them the same way. The OpenTelemetry GenAI conventions are still at Development status; the resource records which conventions were used. Ids derive from the session and step ids, so exporting the same session again produces the same ids.

Commands that the `kernel-tester` and `kernel-worker` subagents run with `run_command` get a W3C `TRACEPARENT` naming their action's span in this trace. A command that emits OpenTelemetry spans of its own therefore joins the exported trace. Pi's native `bash` tool and user `!` commands do not get one: the worker that runs them does not know the host's action id, and a guessed id would point at the wrong span.

No message content is exported by default. `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` exports all of it; `KERNELBRIDGE_OTEL_CAPTURE=prompts,tools,outputs` selects parts. Withheld content is marked `kernelbridge.content.redacted`, never sent as an empty value. Clipped tool outputs carry `kernelbridge.output.truncated` and `original_length`.

## Saved Pi session files

`node source/packages/pi-lattice-trace/bin/lattice-trace.ts SESSION.jsonl [--port N] [--no-open] [--offline] [--otlp]` opens a Pi session file in the same viewer and follows appends. It shows the branch Pi resumes: from the last entry back to the root.

