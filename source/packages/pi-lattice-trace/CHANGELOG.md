# Changelog

## [0.1.0] - 2026-11-12

- `/trace` serves the current Pi session's live trajectory in the Lattice Trace viewer on 127.0.0.1.
- Projection of Pi session entries: model calls with provider token counts and cache hits, tool calls and results, inputs, `!` commands, compactions, model and thinking changes, file history from `write` and `edit`.
- Executing tools and streaming replies appear live; one server follows `/new`, `/resume`, `/fork` and `/tree`.
- `--no-open`, `--offline`, `--port`; SSH sessions get a port-forwarding hint instead of a browser.
