"""Bounded shell commands and isolated Git worktrees for subagents.

Commands run without credentials: a scrubbed environment with a private HOME.
A writing subagent works in its own worktree and branch; the parent decides
whether to apply that branch. Nothing here pushes, merges or touches remotes.
"""
from __future__ import annotations

import os
import signal
import selectors
import shlex
from collections.abc import Callable
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


def run_command_tool(root: Path, cancelled: Callable[[], bool] = lambda: False) -> FunctionTool:
    """Interruptible process-group execution with bounded output and per-call HOME."""
    def run(action: Action) -> Outcome:
        command = action.arguments["command"]
        timeout = action.arguments.get("timeout", 600)
        started = time.monotonic()
        tail = bytearray()
        total = 0
        timed_out = stopped = False
        with tempfile.TemporaryDirectory(prefix="kb-child-home-") as home:
            if cancelled():
                return Outcome(status="failed", data={"reason": "cancelled", "unexecuted": True,
                    "command": command, "cancelled": True, "output": ""})
            process = subprocess.Popen(["/bin/sh", "-c", command], cwd=root, env=command_environment(Path(home)),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            killed = False
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while selector.get_map() or process.poll() is None:
                        timed_out = timed_out or time.monotonic() - started >= timeout
                        stopped = stopped or cancelled()
                        if (timed_out or stopped) and not killed:
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            killed = True
                        for key, _ in selector.select(0.05):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            total += len(chunk)
                            tail.extend(chunk)
                            if len(tail) > MAX_OUTPUT:
                                del tail[:-MAX_OUTPUT]
                process.wait(timeout=5)
            finally:
                # Also reap descendants when the shell exited after launching a
                # background command with redirected output. A finished permit
                # must never leave a process writing into its former worktree.
                try:
                    if not killed:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if process.poll() is None:
                        process.wait(timeout=5)
                finally:
                    process.stdout.close()
        text = tail.decode("utf-8", "replace")
        return Outcome(data={"command": command, "exit_code": process.returncode, "timed_out": timed_out,
            "cancelled": stopped, "output": ("…" if total > MAX_OUTPUT else "") + text,
            "truncated": total > MAX_OUTPUT, "elapsed_ms": int((time.monotonic() - started) * 1000)})

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

    def descriptor(self) -> dict:
        return {"project": str(self.top), "worktree": str(self.path), "branch": self.branch, "base": self.start}

    @classmethod
    def recover(cls, descriptor: dict) -> Worktree:
        """Reopen only the original journal-bound worktree, without creating one."""
        tree = cls.__new__(cls)
        tree.top, tree.path = Path(descriptor["project"]), Path(descriptor["worktree"])
        tree.branch, tree.start = descriptor["branch"], descriptor["base"]
        if (git(tree.path, "rev-parse", "--show-toplevel").stdout.strip() != str(tree.path)
                or git(tree.path, "branch", "--show-current").stdout.strip() != tree.branch):
            raise ToolFailure("original child worktree identity changed; retained for inspection")
        git(tree.top, "rev-parse", "--verify", tree.start + "^{commit}")
        return tree

    def finish(self, message: str) -> dict:
        """Retain the complete result on its branch, then release the checkout."""
        if git(self.path, "branch", "--show-current").stdout.strip() != self.branch:
            raise ToolFailure("child changed its worktree branch; retained for inspection")
        git(self.path, "add", "-A")
        if git(self.path, "diff", "--cached", "--quiet", check=False).returncode != 0:
            git(self.path, "-c", "user.name=KernelBridge", "-c", "user.email=kernelbridge@localhost",
                "commit", "-q", "--no-verify", "-m", message)
        changed = git(self.path, "diff", "--quiet", self.start, "HEAD", check=False).returncode != 0
        if not changed:
            git(self.top, "worktree", "remove", "--force", str(self.path))
            git(self.top, "branch", "-D", self.branch)
            return {"changed": False, "checkout_removed": True}
        patch = git(self.path, "diff", "--binary", self.start, "HEAD").stdout
        result = {"changed": True, "worktree": str(self.path), "branch": self.branch, "base": self.start,
            "commit": git(self.path, "rev-parse", "HEAD").stdout.strip(),
            "diffstat": git(self.path, "diff", "--stat", self.start, "HEAD").stdout[-4000:],
            "patch": patch[:65536], "patch_truncated": len(patch) > 65536,
            "apply_hint": f"git -C {shlex.quote(str(self.top))} diff --binary {self.start} {self.branch} | git -C {shlex.quote(str(self.top))} apply"}
        git(self.top, "worktree", "remove", "--force", str(self.path))
        result["checkout_removed"] = True
        return result
