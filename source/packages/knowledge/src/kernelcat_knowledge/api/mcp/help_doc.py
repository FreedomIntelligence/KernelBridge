"""Self-description for the CLI-first, MCP-compatible Wiki surface."""
from __future__ import annotations

_TEXT = r"""# KernelCat Wiki contract: CLI-first, MCP-compatible

Local Codex and Claude Code sessions should prefer the installable `kcwiki`
command.  It is offline, emits canonical JSON, uses stable exit codes, and calls
the same application services as this MCP adapter. Every invocation requires an
independent Knowledge module binding and a new absolute startup report path:
`kcwiki --expected-python-modules /protected/modules.json --startup-report /reports/new.json ...`.
The command examples below show the operation suffix after these startup options.

## Recommended local interface

```text
kcwiki search --request query.json
kcwiki show <card-or-revision-id> --view agent|audit|human
kcwiki raw grep --source <source-id> --pattern <regex>
kcwiki raw read --evidence <evidence-id>
kcwiki trajectory append --input delta.json
kcwiki index rebuild
kcwiki doctor
```

## MCP compatibility interface

`wiki_search_v1(request)` returns the same `kernelcat.context-projection/v1`
object as `kcwiki search`. `wiki_append_trajectory_delta(delta)`, raw reads, and
page reads are thin transport adapters over the same shared service.

The old bundled page/card/rule inventory is retired; default reads are empty.
Legacy search is deterministic lexical retrieval. Retired or unknown modes fail
explicitly. It returns attributed data with empty phase-rule content/references,
no generated suggestion and disabled agent execution; historical Q fields are not
current rewards. The filesystem service does not implement v8 shared-database,
Controller snapshot or cutoff integration.

The legacy cann-ask v5.2 tools remain available for existing clients:
`wiki_search(...)`, `wiki_get_page(id)`, `wiki_grep_raw(...)`,
`wiki_read_raw(...)`, and `wiki_help()`.
"""


def help_payload() -> dict:
    return {
        "server": "KernelCat AscendC Wiki",
        "overview": "Deterministic local file/card service with immutable evidence and an MCP adapter",
        "recommended_interface": {
            "transport": "cli",
            "command": "kcwiki",
            "search": "kcwiki search --request query.json",
            "offline": True,
            "required_startup_options": ["--expected-python-modules ABS", "--startup-report ABS"],
        },
        "tiers": {"tier0": "phase_rule", "tier1": "static", "tier2": "dynamic"},
        "tools": {
            "wiki_search": {"signature": "wiki_search(query, phase=None, tags=None, type=None, limit=10, intent=None, progress=None, task_description=None, call_count=0, seen_ids=None, device_feedback=None)", "status": "legacy-compatible"},
            "wiki_get_page": {"signature": "wiki_get_page(id)"},
            "wiki_search_v1": {"signature": "wiki_search_v1(request)", "cli_equivalent": "kcwiki search --request query.json"},
            "wiki_append_trajectory_delta": {"signature": "wiki_append_trajectory_delta(delta)", "cli_equivalent": "kcwiki trajectory append --input delta.json"},
            "wiki_grep_raw": {"signature": "wiki_grep_raw(pattern, scope=None, context=4, glob=None, include_tests=False, max_hits=30)", "cli_equivalent": "kcwiki raw grep ..."},
            "wiki_read_raw": {"signature": "wiki_read_raw(path, start=1, end=None)", "cli_equivalent": "kcwiki raw read ..."},
            "wiki_help": {"signature": "wiki_help()"},
        },
        "text": _TEXT,
    }
