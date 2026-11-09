"""Pinned LoopX CLI adapter. No shell, runtime imports, package installation or retries."""
from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

from kernelcat_executor.contracts.loopx import LoopXBinding
from kernelcat_executor.contracts.models import Json, canonical


class LoopXError(ValueError):
    """An external operation may have taken effect; its caller owns reconciliation."""


def strict_object(raw: bytes) -> Json:
    def pairs(items: list[tuple[str, Any]]) -> Json:
        result: Json = {}
        for key, value in items:
            if key in result:
                raise LoopXError("LoopX returned duplicate JSON keys")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
        canonical(value)  # Reject non-finite values and excessively deep JSON.
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise LoopXError("LoopX returned invalid JSON") from error
    if not isinstance(value, dict) or value.get("ok") is not True:
        # Do not copy CLI diagnostics, credentials or private paths into errors.
        raise LoopXError("LoopX did not return an affirmative result; inspect its local state")
    return value


class LoopXClient:
    def __init__(self, binding: LoopXBinding) -> None:
        self.binding = LoopXBinding.model_validate_json(canonical(binding))
        for name in ("cli", "node", "project", "registry", "runtime_root", "home"):
            path = Path(getattr(binding, name))
            if not path.is_absolute():
                raise ValueError(f"LoopX {name} must be an absolute path")
            if name in {"cli", "node"}:
                if not path.is_file() or not os.access(path, os.X_OK):
                    raise ValueError(f"LoopX {name} must be an installed executable")
            elif name == "registry":
                if not path.is_file():
                    raise ValueError("LoopX registry must already exist; no implicit bootstrap")
            elif not path.is_dir():
                raise ValueError(f"LoopX {name} must be an existing directory")
        # The two allowed executable directories plus standard system utilities.
        # No inherited provider keys, NODE_OPTIONS, PYTHONPATH or proxy settings.
        self.environment = {
            "HOME": binding.home,
            "PATH": os.pathsep.join((str(Path(binding.node).parent),
                                      str(Path(binding.cli).parent),
                                      str(Path(sys.executable).parent), os.defpath)),
            "PYTHONNOUSERSITE": "1",
        }
        self.environment.update({k: os.environ[k] for k in ("LANG", "LC_ALL", "TZ") if k in os.environ})

    def _call(self, args: list[str]) -> Json:
        b = self.binding
        argv = [b.cli, "--registry", b.registry, "--runtime-root", b.runtime_root,
                "--format", "json", *args]
        process = subprocess.Popen(argv, cwd=b.project, env=self.environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        assert process.stdout is not None and process.stderr is not None
        captured: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + b.timeout
        selector = selectors.DefaultSelector()
        clean = False
        try:
            for label, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, label)
            count = 0
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LoopXError("LoopX timed out; do not blindly retry a mutation")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    count += len(chunk)
                    if count > b.max_output_bytes:
                        raise LoopXError("LoopX output exceeds the configured bound")
                    captured[key.data].extend(chunk)
            code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if code:
                raise LoopXError("LoopX command failed; inspect its local state before retry")
            result = strict_object(bytes(captured["stdout"]))
            clean = True
            return result
        except subprocess.TimeoutExpired as error:
            raise LoopXError("LoopX timed out; inspect local state") from error
        finally:
            selector.close()
            # Also reap descendants left behind after a failed CLI operation.
            if not clean or process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            process.stdout.close()
            process.stderr.close()

    def verify_version(self) -> None:
        result = self._call(["version"])
        if (result.get("schema_version") != "loopx_version_v0"
                or result.get("name") != "loopx" or result.get("version") != self.binding.version):
            raise LoopXError("LoopX version differs from the qualified 1.2.3 CLI")

    def todo(self) -> Json:
        b = self.binding
        result = self._call(["todo", "list", "--goal-id", b.goal_id, "--todo-id", b.todo_id])
        items = result.get("todos")
        if (result.get("goal_id") != b.goal_id or not isinstance(items, list) or len(items) != 1
                or not isinstance(items[0], dict) or items[0].get("todo_id") != b.todo_id
                or not isinstance(result.get("project"), str)
                or Path(result["project"]).resolve() != Path(b.project).resolve()):
            raise LoopXError("LoopX Todo or project differs from the explicit binding")
        return cast(Json, items[0])

    def probe(self) -> Json:
        b = self.binding
        self.verify_version()
        todo = self.todo()
        guard = self._call(["quota", "should-run", "--goal-id", b.goal_id,
                            "--agent-id", b.agent_id, "--runtime-profile", "generic_cli"])
        lane = guard.get("agent_lane_next_action")
        if not isinstance(lane, dict):
            lane = {}
        allowed = (guard.get("goal_id") == b.goal_id and guard.get("should_run") is True
                   and lane.get("todo_id") == b.todo_id
                   and lane.get("agent_id") in (None, "", b.agent_id)
                   and todo.get("status") == "open"
                   and todo.get("task_class") == "advancement_task"
                   and todo.get("claimed_by") in (None, "", b.agent_id))
        return {"allowed": allowed, "guard": guard, "todo": todo}

    def claim(self) -> Json:
        b = self.binding
        result = self._call(["todo", "claim", "--goal-id", b.goal_id,
                             "--todo-id", b.todo_id, "--agent-id", b.agent_id,
                             "--claimed-by", b.agent_id])
        self.assert_owner()
        return result

    def assert_owner(self) -> None:
        todo = self.todo()
        if todo.get("status") != "open" or todo.get("claimed_by") != self.binding.agent_id:
            raise LoopXError("LoopX Todo is no longer open and owned by this agent")

    def report(self, evidence: str) -> Json:
        if not evidence.startswith("kernelcat-receipt-sha256:") or len(evidence) != 89:
            raise ValueError("LoopX evidence must be a compact KernelCat receipt digest")
        if any(c not in "0123456789abcdef" for c in evidence[-64:]):
            raise ValueError("invalid receipt digest")
        self.assert_owner()
        b = self.binding
        return self._call(["todo", "update", "--goal-id", b.goal_id, "--todo-id", b.todo_id,
                           "--agent-id", b.agent_id, "--evidence", evidence,
                           "--note", "KernelCat execution recorded; independent acceptance is still required."])

    def refresh(self) -> Json:
        self.assert_owner()
        return self._call(["refresh-state", "--goal-id", self.binding.goal_id,
                           "--agent-id", self.binding.agent_id,
                           "--classification", "kernelcat_execution_recorded",
                           "--recommended-action", "Review recorded task evidence before acceptance.",
                           "--delivery-outcome", "surface_only"])
