# pi-lattice-trace

A Pi package that adds `/trace`: it opens the current session's live trajectory in the browser using the Lattice Trace viewer. The page shows the timeline of model and tool calls, context growth against the compaction threshold, tool outputs, and the history of files the session wrote or edited. It updates while the session runs.

```
/trace                     open the viewer for this session
/trace --no-open           print the URL only
/trace --offline           do not load web fonts (system fonts instead)
/trace --port 8765         fixed port (first call only)
/trace --otlp              send the session once to an OpenTelemetry collector
```

For a saved or running session file outside Pi:

```
node bin/lattice-trace.ts ~/.pi/agent/sessions/.../SESSION.jsonl [--no-open] [--offline] [--port N] [--otlp]
```

## Install

```sh
pi install ./source/packages/pi-lattice-trace       # from a KernelBridge checkout
pi -e ./source/packages/pi-lattice-trace            # try it for one run
```

Requires Pi 1.5.0 or later and Node 22.19 or later. The package has no runtime dependencies.

## What it shows

The trace is a read-only projection of the session branch Pi is on (`ctx.sessionManager.getBranch()`), plus live state Pi has not written yet: tools that are executing and a reply that is streaming.

| Session entry | In the viewer |
| --- | --- |
| assistant message | model step. Input = `usage.input + cacheRead + cacheWrite`, which is the provider's prompt size; output = `usage.output`; real cache hits from `cacheRead`. Starts at the request time, ends when Pi stored the reply |
| tool call and its result | tool step with arguments, output (first 4,000 characters) and error status |
| user message, `!command`, extension message | input step (`!!` commands add no context) |
| compaction | compaction step with the size before and after and the summary |
| model or thinking change, context edit, branch summary | marker on the timeline |
| successful `write` / `edit` | file version for the blame (溯源) view, rebuilt from these calls only |

Step ids come from Pi's entry ids, so a selected step stays selected while the session grows. Tool-output sizes are estimated and then scaled so that the rebuilt context matches each next request's prompt size.

One server serves the whole Pi process. After `/new`, `/resume`, `/fork` or `/tree`, an open page follows the new session at the same URL. The server stops when Pi quits.

## Access and privacy

- The server listens on 127.0.0.1 only, under a random 128-bit path token, and rejects other `Host` headers (DNS rebinding). Responses are `no-store`.
- The page contains tool outputs and file contents. Treat the URL like the session itself and do not share it.
- Over SSH, `/trace` does not try to open a browser; forward the port with `ssh -L PORT:127.0.0.1:PORT HOST`.
- Without `--offline`, the page loads the Geist fonts from Google Fonts. No session data is sent, and the referrer is suppressed.
- Set `PI_LATTICE_NO_BROWSER=1` to never open a browser.

## OpenTelemetry export

`--otlp` posts OpenTelemetry GenAI spans (`invoke_agent`, `chat`, `execute_tool`, with OpenInference kinds) as OTLP/HTTP JSON to `OTEL_EXPORTER_OTLP_ENDPOINT`. No message content is sent unless `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` or `KERNELBRIDGE_OTEL_CAPTURE=prompts,tools,outputs` selects it. See `docs/trace.md` in the KernelBridge repository for the mapping.

## Not shown yet

- Other branches of the session tree. The viewer's branch comparison shows the current branch only.
- Subagent sessions (pi-subagents). Their child sessions are separate files.

## Development

The viewer (`viewer/lattice-viewer.html`, Lattice Trace v15) is vendored unmodified. The server injects the trace and `viewer/live-adapter.js` and applies a short table of exact-match edits. Every anchor must occur exactly once, so a changed viewer fails loudly instead of rendering demo data. KernelBridge's own `/trace` uses the same viewer, page assembly and server (`src/`), with a Journal projection in place of `src/session-trace.ts`.

```sh
node --test validation/tui/pi-lattice-trace.mjs                         # projection, page, server
python3 validation/tui/pi_plugin_trace_scenario.py --output DIR         # real Pi in RPC mode + headless Chrome
```
