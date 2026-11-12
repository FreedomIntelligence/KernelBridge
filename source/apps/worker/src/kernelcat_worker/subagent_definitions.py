"""Claude Code-style subagent definition files (Markdown with frontmatter).

Locations: ~/.kernelbridge/agents/*.md (user) and <project>/.kernelbridge/agents/*.md
(project; overrides user by name). Frontmatter: name, description, optional tools
(read | test | write, or a Claude Code tool list) and model (a configured route).
The body is the agent's instructions.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

NAME = re.compile(r"^[a-z][a-z0-9-]{1,47}$")

# Available in project codegen only: the parent can already edit and run shell there.
PROJECT_AGENTS = (
    {"name": "kernel-tester", "tools": "test", "source": "builtin", "model": None, "prompt": "",
     "description": "runs tests, builds and diagnostics and reports failures with evidence"},
    {"name": "kernel-worker", "tools": "write", "source": "builtin", "model": None, "prompt": "",
     "description": "implements a scoped change in an isolated Git worktree branch and verifies it"},
)


def tools_class(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in {"read", "test", "write"}:
        return lowered
    names = {item.strip().lower() for item in re.split(r"[,\s\[\]\"']+", value) if item.strip()}
    if names & {"write", "edit", "multiedit", "notebookedit", "write_file", "edit_file"}:
        return "write"
    if names & {"bash", "run_command"}:
        return "test"
    return "read"


def parse(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip("\"'")
    body = text[end + 4:].strip()
    name, description = meta.get("name", ""), meta.get("description", "")
    if not NAME.match(name) or not description or not body:
        return None
    return {"name": name, "description": description[:400], "tools": tools_class(meta.get("tools", "")),
            "model": meta.get("model") or None, "prompt": body[:16384]}


def discover(project: Path, home: Path, *, project_mode: bool) -> list[dict]:
    agents: dict[str, dict] = {agent["name"]: dict(agent) for agent in (PROJECT_AGENTS if project_mode else ())}
    for source, directory in (("user", home / "agents"), ("project", project / ".kernelbridge" / "agents")):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                agent = parse(path)
            except (OSError, UnicodeError):
                agent = None
            if agent:
                agents[agent["name"]] = {**agent, "source": source}
    # Worktrees require a real committed project. Do not advertise a capability
    # whose preparation will fail after the delegation has been admitted.
    writable = False
    if project_mode and any(agent["tools"] == "write" for agent in agents.values()):
        try:
            probe = subprocess.run(["git", "-C", str(project), "rev-parse", "--verify", "HEAD"],
                                   capture_output=True, timeout=5)
            writable = probe.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    # Isolated device tasks keep children read-only; their sources are frozen.
    return [agent for agent in agents.values()
            if (project_mode or agent["tools"] == "read") and (agent["tools"] != "write" or writable)]
