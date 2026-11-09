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
    "school": (TRANSPORT, ["-m", "school_sdk"]),
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


def test_plan() -> list[tuple[Path, list[str]]]:
    manifest = json.loads((ROOT / "tests/suites.json").read_text())
    actual = {p.name for p in (TOOLS / "ab_sdk/tests").glob("test_*.py")}
    maintained, historical = manifest["sdk_current"], manifest["sdk_historical"]
    if (len(maintained) != len(set(maintained)) or len(historical) != len(set(historical))
            or set(maintained) & set(historical) or set(maintained) | set(historical) != actual):
        raise ValueError("SDK tests changed: classify every file in tests/suites.json before running the gate")
    if any(Path(name).name != name for name in (*maintained, *historical)):
        raise ValueError("test manifest must contain filenames, not paths")
    return [
        (ROOT, ["-m", "pytest", "-q", "tests/test_boundaries.py", "tests/test_performance_schema.py"]),
        (PIPELINE, ["-m", "pytest", "-q", "tests"]),
        (TOOLS, ["-m", "pytest", "-q", *["ab_sdk/tests/" + name for name in maintained]]),
        (TRANSPORT, ["-m", "pytest", "-q", "school_sdk/tests"]),
        *[(ROOT, [str(ROOT / "tests" / script)]) for script in
          ("test_ast_audit.py", "test_audit_modelnew.py", "test_runtime_guard.py")],
    ]


def run_tests() -> int:
    result = 0
    for cwd, argv in test_plan():
        print("Backend contract suite:", cwd.relative_to(SOURCE), flush=True)
        code = execute(argv, cwd)
        result = result or code
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(*COMMANDS, "evaluate", "batch", "inspect", "catalog", "test"))
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
    if args.command == "test":
        if rest:
            parser.error("test takes no extra arguments")
        return run_tests()
    cwd, command = COMMANDS[args.command]
    return execute([*command, *rest], cwd)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from None
