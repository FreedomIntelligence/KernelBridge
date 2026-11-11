# Subagent prompt adaptation

`source/apps/agent/src/harness/pi/delegation-prompt.mjs` (parent guidance) and the `delegate` tool description in `source/packages/executor/src/kernelcat_executor/capabilities/delegation.py` are adapted from published coding-agent practice. They are rewritten for KernelBridge's tools and authority model rather than copied.

## Sources

| Source | Version | License | Used for |
| --- | --- | --- | --- |
| [openai/codex](https://github.com/openai/codex) multi-agent tool definitions (`spawn_agent`, `wait_agent`, `list_agents`, `close_agent`, agent role files) | commit `9018f3a3b021082ef186ddd6503c0b4fd7a26260` | Apache-2.0 | role files with `description` + instructions; global concurrency and depth bounds instead of per-agent step limits |
| [Piebald-AI/claude-code-system-prompts](https://github.com/Piebald-AI/claude-code-system-prompts): Agent tool "when to launch", subagent guidance, writing subagent prompts | Claude Code 2.1.53–2.1.290 extracts | MIT | launching independent agents concurrently in one message, self-contained briefs, not repeating delegated searches, synthesizing reports; Markdown agent definitions |

Retained copies used during the adaptation are under `.validation/prompt-sources/` with their license files. They are reference material, not runtime inputs.

## Deliberate differences

- Children are host-governed: every child model and tool call is admitted, budgeted and receipted by the KernelBridge host. Child output is advisory, never correctness or device evidence.
- Agent types are `read`, `test` (adds `run_command`) and `write` (an isolated Git worktree branch). A writing child never changes the parent's files; the parent reviews and applies its diff.
- There is no blocking wait tool. Background children announce their result as a `[子代理结果]` message (as Claude Code background agents notify), because a blocking wait would hold the host executor that background children need.
- There is no inherited-history fork child; `/fork` is a user conversation branch.
- Limits follow Claude Code and Codex: a concurrency bound (`max_concurrent`), no default per-child step, read or time limits. Near the context bound the child is asked for a final summary.
- User-visible text (descriptions, notifications) is Chinese to match the product UI; model-facing guidance stays in English.
