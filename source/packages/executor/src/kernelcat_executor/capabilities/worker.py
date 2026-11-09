"""Bounded, sequential NDJSON transport. No model loop, credentials or shell."""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import time
from collections.abc import Callable, Container
from pathlib import Path
from typing import Any

from kernelcat_executor.contracts.json_decoder import WorkerError as WorkerError
from kernelcat_executor.contracts.json_decoder import decode_object as decode_object

PROTOCOL = "kernelcat.pi-worker/v3"
METHODS = frozenset({"hello", "prepare_turn", "model", "tool", "batch", "end_turn", "complete",
                     "plugin_begin", "child_model", "child_tool", "plugin_settle",
                     "native_begin", "native_call", "native_settle",
                     "rsi_status", "rsi_list", "rsi_read", "rsi_record", "rsi_effect", "rsi_boundary", "rsi_rounds",
                     "rsi_adapter", "rsi_apply", "rsi_context"})


def decode_request(raw: bytes, expected_id: int, *,
                   methods: Container[str] = METHODS) -> tuple[str, dict[str, Any]]:
    request = decode_object(raw)
    if (set(request) != {"protocol", "id", "method", "params"}
            or request["protocol"] != PROTOCOL
            or type(request["id"]) is not int or request["id"] != expected_id
            or not isinstance(request["method"], str) or request["method"] not in methods
            or not isinstance(request["params"], dict)):
        raise WorkerError("invalid worker envelope or out-of-order request")
    return request["method"], request["params"]


def run_worker(
    argv: tuple[str, ...], cwd: Path,
    handle: Callable[[str, dict[str, Any]], dict[str, Any]], *,
    timeout: float = 300.0, max_frame_bytes: int = 1048576,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Serve one worker. Effects settle in handle(), not in this transport.

    Deadline/cancellation is checked between admitted effects. Each provider must
    enforce its own operation timeout. Killing this child cannot cancel a remote
    device job, nor does EOF establish that a dispatched effect did not happen.
    """
    if (not argv or not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file()
            or timeout <= 0 or not 4096 <= max_frame_bytes <= 16777216):
        raise WorkerError("invalid worker executable or transport limits")
    if not cwd.is_absolute() or not cwd.is_dir():
        raise WorkerError("worker cwd must be an existing absolute directory")
    # Deliberately omit HOME, PATH, NODE_OPTIONS, proxy settings and provider keys.
    env = {key: os.environ[key] for key in ("LANG", "LC_ALL", "TZ") if key in os.environ}
    env["KERNELCAT_MAX_FRAME_BYTES"] = str(max_frame_bytes)
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
        start_new_session=os.name == "posix")
    assert process.stdin is not None and process.stdout is not None
    deadline = time.monotonic() + timeout
    incoming = bytearray()
    request_id = 1

    def remaining() -> float:
        if cancelled():
            raise WorkerError("worker cancelled; admitted effects require receipt inspection")
        left = deadline - time.monotonic()
        if left <= 0:
            raise WorkerError("worker deadline exceeded; inspect receipts before resuming")
        return min(left, 0.1)

    try:
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stdin.fileno(), False)
        while True:
            remaining()
            newline = incoming.find(b"\n")
            if newline < 0:
                if len(incoming) > max_frame_bytes:
                    raise WorkerError("worker request exceeds frame limit")
                ready, _, _ = select.select([process.stdout], [], [], remaining())
                if not ready:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise WorkerError("worker exited before protocol completion")
                incoming.extend(chunk)
                continue
            if newline > max_frame_bytes:
                raise WorkerError("worker request exceeds frame limit")
            raw = bytes(incoming[:newline])
            del incoming[:newline + 1]
            method, params = decode_request(raw, request_id)
            result = handle(method, params)
            reply = json.dumps({"protocol": PROTOCOL, "id": request_id, "result": result},
                               ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
            if len(reply) > max_frame_bytes:
                raise WorkerError("worker response exceeds frame limit; receipts remain authoritative")
            outgoing = memoryview(reply + b"\n")
            while outgoing:
                _, ready, _ = select.select([], [process.stdin], [], remaining())
                if ready:
                    count = os.write(process.stdin.fileno(), outgoing)
                    outgoing = outgoing[count:]
            request_id += 1
            if method == "complete":
                process.stdin.close()
                try:
                    code = process.wait(timeout=max(0.001, min(5.0, deadline - time.monotonic())))
                except subprocess.TimeoutExpired as error:
                    raise WorkerError("worker failed to exit after completion") from error
                if code != 0 or incoming:
                    raise WorkerError("worker did not complete cleanly")
                return result
    except (BrokenPipeError, OSError) as error:
        raise WorkerError("worker transport interrupted; inspect receipts before resuming") from error
    finally:
        if process.poll() is None:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
        process.wait()
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()
