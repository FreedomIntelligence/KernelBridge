# Operator instructions, scratch and online research: source comparison

Reviewed on 2026-11-12 before resuming implementation. This is an implementation
comparison, not a claim of complete Codex or Claude Code compatibility. The
ongoing DeepSeek trial uses its original source and frozen configuration; these
changes apply only to newly prepared sessions.

## Official source boundaries

| Repository | Pinned commit / license | What was checked |
| --- | --- | --- |
| [openai/codex](https://github.com/openai/codex/tree/c0cc9fc820f62e4dbdde68214dd6d792d6c8a7ee) | `c0cc9fc820f62e4dbdde68214dd6d792d6c8a7ee`, Apache-2.0 | Real permission lowering, instruction discovery, role schemas, child notifications and web-search client |
| [anthropics/sandbox-runtime](https://github.com/anthropics/sandbox-runtime/tree/41501a60b27deb89484b718acb76a04e2a37aa75) | `41501a60b27deb89484b718acb76a04e2a37aa75`, Apache-2.0 | Real macOS/Linux sandbox implementation, writable roots, temporary paths and deny rules |
| [anthropics/claude-plugins-official](https://github.com/anthropics/claude-plugins-official/tree/e0ab6e51219eef74e417a0e1d89e5d53e5e763d4) | `e0ab6e51219eef74e417a0e1d89e5d53e5e763d4`, Apache-2.0 | Published feature-dev agent definitions and their declared tools |
| [anthropics/claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python/tree/3cf3446b0fa6c97042e85b245bba07459f22f946) | `3cf3446b0fa6c97042e85b245bba07459f22f946`, MIT | Public CLI bridge and MCP dispatch; not the private CLI implementation |
| [anthropics/claude-code](https://github.com/anthropics/claude-code/tree/927b66497b89542dc1242e26d5735dbf880c829e) | `927b66497b89542dc1242e26d5735dbf880c829e`, all rights reserved | Public mods/hooks, examples and documentation; the complete CLI core is absent |

The public Codex search client invokes a hosted service. Claude's built-in web
tools also depend on CLI/hosted components. Neither source set provides a
provider-neutral search backend that can simply be transplanted into Pi.

## Decisions used in KernelBridge

| Concern | Source evidence | KernelBridge decision |
| --- | --- | --- |
| Workspace scratch | [Codex writable roots and temporary paths](https://github.com/openai/codex/blob/c0cc9fc820f62e4dbdde68214dd6d792d6c8a7ee/codex-rs/protocol/src/permissions.rs#L837-L882), [Anthropic writable policy](https://github.com/anthropics/sandbox-runtime/blob/41501a60b27deb89484b718acb76a04e2a37aa75/src/sandbox/sandbox-manager.ts#L2814-L2848) | Allow helper files inside a clean operator workspace while retaining protected references, task instructions and actual agent-autoload paths. Candidate source identity remains separate. |
| Read-only paths | [Codex exact and descendant exclusions](https://github.com/openai/codex/blob/c0cc9fc820f62e4dbdde68214dd6d792d6c8a7ee/codex-rs/sandboxing/src/seatbelt.rs#L547-L555), [ancestor rename protection](https://github.com/openai/codex/blob/c0cc9fc820f62e4dbdde68214dd6d792d6c8a7ee/codex-rs/sandboxing/src/seatbelt.rs#L863-L883) | Preserve native restrictions and complete-diff authorization before writeback, including protected paths that do not exist yet. |
| Instruction delivery | [Codex discovery and precedence](https://github.com/openai/codex/blob/c0cc9fc820f62e4dbdde68214dd6d792d6c8a7ee/codex-rs/core/src/agents_md.rs#L196-L301), [Claude documented loading](https://code.claude.com/docs/en/memory#how-claude-md-files-load) | Freeze the explicitly admitted task instructions and deliver them to the real child provider's system message. Do not silently load ancestors, home files or the parent's chat into isolated experiments. |
| Configurable roles | [Codex runtime schema](https://github.com/openai/codex/blob/c0cc9fc820f62e4dbdde68214dd6d792d6c8a7ee/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L108-L163) | Describe actual configured role permissions and expose only available parameters/tools. Never derive authority from a built-in-looking role name. |
| Web research | [Codex client request](https://github.com/openai/codex/blob/c0cc9fc820f62e4dbdde68214dd6d792d6c8a7ee/codex-rs/ext/web-search/src/tool.rs#L112-L175), [Claude tool behavior](https://code.claude.com/docs/en/tools-reference#websearch-tool-behavior) | Implement an independent explicit-provider adapter. Send only a requested query/URL; retain actual responses, URLs, timestamps and hashes. Failures and unavailable providers remain visible. |
| Candidate acceptance | No corresponding generic coding-agent mechanism is asserted | Recover the complete workspace, but accept F/G/H only against the declared runtime source bundle and original evaluator, contract and receipts. Helper files do not become measured candidate inputs merely because they share a workspace. |

KernelBridge's isolated shell still copies a bounded workspace per invocation,
persists authorized changes only on a successful exit, and deletes its private
TMPDIR afterward. Codex's live writable roots do not have that transaction
behavior. The current leaf/fresh-context child model and explicit task-file
loading also differ from upstream fork/history and project discovery. These
differences are intentional boundaries or separate pending features, not
equivalence claims.

## Guidance recovered from operator sessions

The local `infra/AGENTS.operator-optimization.md` was already bundled in the CANN
example. Clean scatter campaign preparation omitted it; the new preparation
copies that generic guidance and a task-specific appendix into admitted files.
The appendix describes actual binding inputs, diagnostic driver IDs, artifact
paging and delegation evidence boundaries without supplying an implementation.

A bounded replay of two original main-session segments recovered 20 web query
and result pairs. It found searches of official SDK semantics and architecture
support, full-output contracts, compiler errors, public dispatch/tiling code,
and profiler field definitions. Sources included CANN documentation and
ops-math, PyTorch, CCCL, vLLM/vLLM-Ascend, DeepSelect, TileKernels, FlashInfer and
SGLang. Failed fetches and empty searches remain failed observations. Separate
Cummin source manifests support provenance, not a claim that its original
retrieval session was replayed.

The portable guidance therefore triggers research on a concrete evidence gap:
unknown API semantics, missing target-version support, an exact build error,
an untested structural mechanism or an ambiguous diagnostic field. It asks for
primary sources, applicability checks and retained evidence. It does not force
a web search every iteration, inject previous operator answers, or count child
advice and search snippets as device validation.
