"""Explicit single/batch evaluation requests; no generator layout or shared build directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

from kernelcat_executor.evaluation.protocols.six_source.config import BACKEND_ROOT, Request


def _write(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, allow_nan=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _execute(argv: list[str], cwd: Path, env: dict, log: Path, timeout: int) -> tuple[int | None, str | None]:
    with log.open("xb") as stream:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stream, stderr=stream,
                                   start_new_session=True)
        try:
            return process.wait(timeout=timeout), None
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            return None, "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "cancelled"


def run_job(request: Request) -> dict:
    """Run only after an operator authorized native execution. Existing attempts are never retried."""
    request = Request.from_dict(request.payload())
    data = {name: getattr(request, name).read_bytes() for name in ("candidate", "reference")}
    identity = {name + "_sha256": hashlib.sha256(value).hexdigest() for name, value in data.items()}
    # Atomic ownership: even a crashed/partial attempt cannot be overwritten by a retry.
    request.workspace.mkdir(parents=True, exist_ok=False)
    record = {"schema_version": "kernelcat.ascend-job/v1", "request": request.payload(), **identity}
    _write(request.workspace / "request.json", record)
    for key, content in data.items():
        (request.workspace / (key + ".py")).write_bytes(content)
    build = request.workspace / "build"
    shutil.copytree(BACKEND_ROOT / "templates" / "CppExtension", build / "CppExtension",
                    ignore=shutil.ignore_patterns("build", "dist", "__pycache__", "*.so", "*.egg-info"))
    env = os.environ.copy()
    env.update(ASCEND_DEVICE_ID=str(request.device), ASCENDC_DEVICE=request.soc,
               OP_ENGINEER_DIR=str(build), PYTHONPATH=str(BACKEND_ROOT.parents[2]), PYTHON=sys.executable)
    customize = build / "opp/vendors/customize"
    env["ASCEND_CUSTOM_OPP_PATH"] = str(customize)
    env["LD_LIBRARY_PATH"] = str(customize / "op_api/lib") + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    start = time.monotonic()
    result: dict = {"status": "unknown", "data": {**identity, "artifact": str(request.workspace), "promoted": False}}
    try:
        code, interruption = _execute([sys.executable, "-m", "kernelcat_executor.evaluation.protocols.six_source.runner", "--request",
            str(request.workspace / "request.json")], request.workspace, env,
            request.workspace / "runner.log", request.timeout)
        native = request.workspace / "native-result.json"
        if interruption or code != 0 or not native.is_file():
            result["data"].update(error=interruption or "runner_did_not_return_authoritative_result", returncode=code)
        else:
            value = json.loads(native.read_text())
            if not isinstance(value, dict) or type(value.get("compiled")) is not bool or type(value.get("correctness")) is not bool:
                raise ValueError("invalid native result contract")
            result["status"] = "completed" if value["compiled"] and value["correctness"] else "failed"
            result["data"]["result"] = value
    except Exception as exc:
        result["data"].update(error=type(exc).__name__, message=str(exc))
    result["data"]["wall_seconds"] = time.monotonic() - start
    _write(request.workspace / "result.json", result)
    return result


def run_batch(requests: list[Request], workers: int) -> list[dict]:
    if not requests or workers < 1:
        raise ValueError("batch needs at least one request and one worker")
    paths = [r.workspace for r in requests]
    if len(set(paths)) != len(paths) or any(a != b and a.is_relative_to(b) for a in paths for b in paths):
        raise ValueError("batch workspaces must be unique and disjoint")
    if any(path.exists() for path in paths):
        raise FileExistsError("batch contains an existing attempt; inspect before making a new request")
    devices = {r.device: Lock() for r in requests}
    def one(request):
        # Never concurrently schedule two batch jobs onto the same configured device.
        with devices[request.device]:
            return run_job(request)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(one, requests))


def main(argv: list[str] | None = None, *, batch: bool = False) -> int:
    parser = argparse.ArgumentParser(description="Explicit six-source evaluation; run in a provisioned isolated native environment.")
    parser.add_argument("--request", type=Path, required=True, help="JSON request, or batch object with a jobs array")
    parser.add_argument("--ack-isolated-runner", action="store_true")
    if batch:
        parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args(argv)
    if not args.ack_isolated_runner:
        parser.error("operator must provision isolation and pass --ack-isolated-runner")
    try:
        value = json.loads(args.request.read_text())
        if batch:
            if not isinstance(value, dict) or set(value) != {"jobs"} or not isinstance(value["jobs"], list):
                raise ValueError("batch JSON must contain only a jobs array")
            requests = [Request.from_dict(v, args.request.resolve().parent) for v in value["jobs"]]
            results = run_batch(requests, args.workers)
            output = {"status": "completed" if all(r["status"] == "completed" for r in results) else "failed", "jobs": results}
        else:
            output = run_job(Request.from_dict(value, args.request.resolve().parent))
        print(json.dumps(output, allow_nan=False))
        return 0 if output["status"] == "completed" else 1
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__, "message": str(exc)}))
        return 2
