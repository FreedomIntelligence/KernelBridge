#!/usr/bin/env python3
"""The sole public Ascend dispatcher. Native protocol details stay behind this boundary."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PROTOCOLS = ROOT.parents[1] / "evaluation/protocols"
PIPELINE = PROTOCOLS / "cannbench/native"
TRANSPORT = ROOT / "transport"
SOURCE = ROOT.parents[2]
TOOLS = PIPELINE / "r950_tools"
# Plain data rather than a plugin framework. CANNBench and six-source semantics stay separate.
COMMANDS = {
    "ab": (TOOLS, [str(TOOLS / "abctl")]),
    "control": (PIPELINE, [str(PIPELINE / "kcatctl.py")]),
    "profile": (TOOLS, [str(TOOLS / "profiler_run.py")]),
    "score": (TOOLS, [str(TOOLS / "kernel_c_score.py")]),
    "audit": (ROOT, [str(ROOT / "tools/check_hack.py")]),
    "schema": (ROOT, ["-m", "kernelcat_executor.evaluation.protocols.six_source.native.schema"]),
}


def environment() -> dict[str, str]:
    env = os.environ.copy()
    # Only child processes get native import roots; never mutate the host import graph.
    env["PYTHONPATH"] = os.pathsep.join((str(SOURCE), str(ROOT), str(TRANSPORT), str(TOOLS)))
    env["KERNELCAT_REPO_ROOT"] = str(PROTOCOLS / "cannbench")
    return env


def execute(argv: list[str], cwd: Path) -> int:
    return subprocess.run([sys.executable, *argv], cwd=cwd, env=environment(), check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(*COMMANDS, "evaluate", "batch", "inspect", "catalog"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    rest = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    if args.command in {"evaluate", "batch"}:
        from kernelcat_executor.evaluation.evaluate import main as evaluate
        return evaluate(rest, batch=args.command == "batch")
    if args.command == "inspect":
        inspect = argparse.ArgumentParser(description="Read a completed job; never retry an uncertain execution.")
        inspect.add_argument("--workspace", type=Path, required=True)
        location = inspect.parse_args(rest).workspace
        path = location / "result.json"
        result = json.loads(path.read_text()) if path.is_file() else {"status": "unknown", "workspace": str(location)}
        print(json.dumps(result, allow_nan=False))
        return 0
    if args.command == "catalog":
        from kernelcat_executor.evaluation.protocols.six_source.catalog import dataset
        print(json.dumps(dataset, sort_keys=True))
        return 0
    cwd, command = COMMANDS[args.command]
    return execute([*command, *rest], cwd)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from None
