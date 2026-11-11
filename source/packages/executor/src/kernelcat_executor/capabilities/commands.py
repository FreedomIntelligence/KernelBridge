"""Bounded shell commands and isolated Git worktrees for subagents.

Commands run without credentials: a scrubbed environment with a private HOME.
A writing subagent works in its own worktree and branch; the parent decides
whether to apply that branch. Nothing here pushes, merges or touches remotes.
"""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from kernelcat_executor.capabilities.workspace_tools import FunctionTool
from kernelcat_executor.contracts.models import Action, Outcome, ToolFailure, ToolSpec, digest

ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ")
MAX_OUTPUT = 64 * 1024


def command_environment(home: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in ENV_KEYS if key in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.update(HOME=str(home), TMPDIR=tempfile.gettempdir(), TERM="dumb", CI="1", NO_COLOR="1",
               GIT_TERMINAL_PROMPT="0")
    return env


def run_command_tool(root: Path) -> FunctionTool:
    """A non-interactive command in root; output keeps the last 64 KB."""
    home = Path(tempfile.mkdtemp(prefix="kb-child-home-"))

    def run(action: Action) -> Outcome:
        command = action.arguments["command"]
        timeout = action.arguments.get("timeout", 600)
        started = time.monotonic()
        process = subprocess.Popen(["/bin/sh", "-c", command], cwd=root, env=command_environment(home),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        timed_out = False
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate()
        text = output.decode("utf-8", "replace")
        truncated = len(text) > MAX_OUTPUT
        return Outcome(data={"command": command, "exit_code": process.returncode, "timed_out": timed_out,
                             "output": ("…" + text[-MAX_OUTPUT:]) if truncated else text, "truncated": truncated,
                             "elapsed_ms": int((time.monotonic() - started) * 1000)})

    return FunctionTool(ToolSpec(name="run_command", credits=1, description=(
        "Run a non-interactive shell command in the working directory (tests, builds, diagnostics). "
        "No credentials are available; output keeps the last 64 KB; timeout in seconds (default 600)."),
        parameters={"type": "object", "additionalProperties": False, "required": ["command"],
                    "properties": {"command": {"type": "string", "minLength": 1, "maxLength": 8192},
                                   "timeout": {"type": "integer", "minimum": 1, "maximum": 3600}}}), run)


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120,
                            env=command_environment(Path(tempfile.gettempdir())))
    if check and result.returncode != 0:
        raise ToolFailure(f"git {args[0]} failed: {result.stderr.strip()[:400]}")
    return result


class Worktree:
    """The parent's current files (committed plus uncommitted tracked changes) on a new branch."""

    def __init__(self, project: Path, identity: str, home: Path) -> None:
        top = git(project, "rev-parse", "--show-toplevel", check=False)
        if top.returncode != 0:
            raise ToolFailure("a writing subagent requires a Git project")
        self.top = Path(top.stdout.strip())
        head = git(self.top, "rev-parse", "HEAD", check=False)
        if head.returncode != 0:
            raise ToolFailure("a writing subagent requires at least one commit")
        self.start = git(self.top, "stash", "create").stdout.strip() or head.stdout.strip()
        short = digest(identity)[:10]
        self.branch = f"kernelbridge/agent-{short}"
        self.path = home / "worktrees" / digest(str(self.top))[:12] / short
        self.path.parent.mkdir(parents=True, exist_ok=True)
        git(self.top, "worktree", "add", "-q", "-b", self.branch, str(self.path), self.start)

    def finish(self, message: str) -> dict:
        """Commit the child's changes on its branch; remove an unchanged worktree."""
        git(self.path, "add", "-A")
        if git(self.path, "diff", "--cached", "--quiet", check=False).returncode == 0:
            git(self.top, "worktree", "remove", "--force", str(self.path), check=False)
            git(self.top, "branch", "-D", self.branch, check=False)
            return {"changed": False}
        git(self.path, "-c", "user.name=KernelBridge", "-c", "user.email=kernelbridge@localhost",
            "commit", "-q", "--no-verify", "-m", message)
        patch = git(self.path, "diff", self.start, "HEAD").stdout
        return {"changed": True, "worktree": str(self.path), "branch": self.branch, "base": self.start,
                "diffstat": git(self.path, "diff", "--stat", self.start, "HEAD").stdout[-4000:],
                "patch": patch[:65536], "patch_truncated": len(patch) > 65536,
                "apply_hint": f"git -C {self.top} diff --binary {self.start} {self.branch} | git -C {self.top} apply"}
