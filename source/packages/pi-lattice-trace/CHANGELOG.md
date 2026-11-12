# Changelog

## [0.2.0] - 2026-11-12

- `--otlp` exports the session as OpenTelemetry GenAI spans (OTLP/HTTP JSON); no message content unless selected.
- `bin/lattice-trace.ts` opens a saved Pi session file and follows appends.
- Branch comparison: other branches of the session tree become fork lanes; the viewer orders the fork prefix by timeline, not by id text.
- Fixed: the branch history drew NaN coordinates for a session with one lane; the divergence summary failed when a branch began with an input.
- Model steps record cache writes and reasoning tokens; clipped tool outputs record `truncated` and `originalLength`; tool steps record the provider call id.

## [0.1.0] - 2026-11-12

- `/trace` serves the current Pi session's live trajectory in the Lattice Trace viewer on 127.0.0.1.
- Projection of Pi session entries: model calls with provider token counts and cache hits, tool calls and results, inputs, `!` commands, compactions, model and thinking changes, file history from `write` and `edit`.
- Executing tools and streaming replies appear live; one server follows `/new`, `/resume`, `/fork` and `/tree`.
- `--no-open`, `--offline`, `--port`; SSH sessions get a port-forwarding hint instead of a browser.
