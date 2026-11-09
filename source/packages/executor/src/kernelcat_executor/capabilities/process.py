"""Bounded evaluator transport. Process isolation here is NOT a native-code sandbox."""
from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.contracts.models import (
    Action,
    Json,
    Outcome,
    ToolFailure,
    ToolSpec,
    canonical,
    digest,
)


@dataclass(frozen=True)
class ProcessResult:
    stdout: bytes
    stderr: bytes
    returncode: int
    reason: str | None


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def collect_process(argv: tuple[str, ...], cwd: Path, environment: dict[str, str], request: bytes,
                    timeout: float, output_limit: int, error_limit: int) -> ProcessResult:
    """Concurrent nonblocking I/O; neither PIPE memory nor temp-file disk can grow unbounded."""
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd=cwd, env=environment,
                               start_new_session=True, close_fds=True)
    output = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": output_limit, "stderr": error_limit}
    reason = None
    deadline = time.monotonic() + timeout
    pipes = (process.stdin, process.stdout, process.stderr)
    try:
        with selectors.DefaultSelector() as selector:
            for name, pipe in zip(("stdin", "stdout", "stderr"), pipes, strict=True):
                assert pipe is not None
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe.fileno(), selectors.EVENT_WRITE if name == "stdin"
                                  else selectors.EVENT_READ, name)
            offset = 0
            while selector.get_map() and reason is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = "evaluator_timeout"
                    break
                for key, _ in selector.select(min(remaining, 0.05)):
                    name = key.data
                    if name == "stdin":
                        try:
                            offset += os.write(key.fd, request[offset:offset + 65536])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            offset = len(request)
                        if offset >= len(request):
                            selector.unregister(key.fd)
                            assert process.stdin is not None
                            process.stdin.close()
                    else:
                        try:
                            chunk = os.read(key.fd, 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fd)
                            continue
                        available = limits[name] - len(output[name])
                        output[name].extend(chunk[:available])
                        if len(chunk) > available:
                            reason = "evaluator_output_limit"
                            break
            if reason:
                _kill_group(process)
            try:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = "evaluator_timeout"
                _kill_group(process)
                process.wait(timeout=5)
        return ProcessResult(bytes(output["stdout"]), bytes(output["stderr"]), int(process.returncode) if process.returncode is not None else -1, reason)
    finally:
        # Also clean up children left behind by a wrapper that exited early.
        _kill_group(process)
        for pipe in pipes:
            if pipe is not None:
                pipe.close()
        if process.poll() is None:
            process.wait(timeout=5)


def _persist(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class CommandEvaluator:
    """Registered argv + JSON contract; no interpolation or automatic execution retry.

    Optional artifacts live outside the writable workspace. A preexisting attempt
    directory is unresolved evidence, never permission to overwrite/re-execute.
    """
    def __init__(self, argv: tuple[str, ...], workspace: Workspace, parameters: Json,
                 credits: int = 10, timeout: float = 120,
                 environment: dict[str, str] | None = None, artifact_root: Path | None = None,
                 output_limit: int = 1048576, error_limit: int = 262144) -> None:
        if not argv or not Path(argv[0]).is_absolute():
            raise ValueError("evaluator executable must be an absolute configured path")
        if timeout <= 0 or output_limit <= 0 or error_limit <= 0:
            raise ValueError("evaluator limits must be positive")
        self.argv, self.workspace, self.timeout = argv, workspace, timeout
        self.environment = dict(environment or {})
        self.artifact_root = artifact_root.resolve() if artifact_root is not None else None
        if self.artifact_root and (self.artifact_root.is_relative_to(workspace.root)
                                  or workspace.root.is_relative_to(self.artifact_root)):
            raise ValueError("evaluator evidence must be disjoint from writable source")
        self.output_limit, self.error_limit = output_limit, error_limit
        self.spec = ToolSpec(name="evaluate", description="Run the registered isolated evaluator.",
                             parameters=parameters, credits=credits)

    def execute(self, action: Action) -> Outcome:
        request = canonical(action).encode()
        if len(request) > 1048576:
            raise ToolFailure("evaluator input exceeds 1 MiB; use a source artifact path")
        directory = None
        if self.artifact_root:
            directory = self.artifact_root / digest(action.id)
            try:
                directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            except FileExistsError:
                return Outcome(status="unknown", data={"reason": "existing_evaluator_attempt"})
            _persist(directory / "action.json", request)
        result = collect_process(self.argv, self.workspace.root, self.environment, request,
                          self.timeout, self.output_limit, self.error_limit)
        execution = {
            "returncode": result.returncode, "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
            "stdout_bytes": len(result.stdout), "stderr_bytes": len(result.stderr),
            "output_limit_reached": result.reason == "evaluator_output_limit",
            "artifact_id": digest(action.id) if directory else None,
        }
        if directory:
            _persist(directory / "stdout.bin", result.stdout)
            _persist(directory / "stderr.bin", result.stderr)
        if result.reason or result.returncode != 0:
            outcome = Outcome(status="unknown", data={
                "reason": result.reason or "evaluator_protocol_failure"})
        else:
            try:
                envelope = json.loads(result.stdout)
                if not isinstance(envelope, dict) or "status" not in envelope or "data" not in envelope:
                    raise ValueError("evaluator must provide an explicit outcome envelope")
                outcome = Outcome.model_validate_json(result.stdout)
                canonical(outcome)  # Reject non-finite numbers before authoritative settlement.
            except ValueError:
                outcome = Outcome(status="unknown", data={"reason": "invalid_evaluator_response"})
        outcome = outcome.model_copy(update={"data": {**outcome.data, "execution": execution}})
        if directory:
            _persist(directory / "outcome.json", canonical(outcome).encode())
        return outcome
