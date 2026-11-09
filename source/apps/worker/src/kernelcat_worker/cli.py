"""Explicit worker entry; stdout is reserved for the finite RPC stream."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kernelcat_executor.contracts.models import HarnessError, canonical

from kernelcat_worker.compose import serve, serve_runtime
from kernelcat_worker.lifecycle import termination_boundary


def main(argv: list[str] | None = None, *, expected_modules: object | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] == "ingest":
        from kernelcat_worker.ingestion import main as ingestion_main
        return ingestion_main(values[1:], expected_modules=expected_modules)
    if values and values[0] == "operator-history":
        from kernelcat_worker.operator_history import main as history_main
        return history_main(values[1:], expected_modules=expected_modules)
    parser = argparse.ArgumentParser(prog="kernelcat-worker")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("serve", help="serve one finite ExecutionRPC session over stdio")
    command.add_argument("--config", type=Path, required=True)
    command.add_argument("--task", type=Path, required=True)
    runtime = commands.add_parser("serve-runtime", help="finite managed Run bootstrap and execution")
    runtime.add_argument("--config", type=Path, required=True)
    runtime.add_argument("--manifest", type=Path)
    args = parser.parse_args(values)
    try:
        with termination_boundary():
            if args.command == "serve-runtime":
                serve_runtime(args.config, args.manifest, sys.stdin.buffer, sys.stdout.buffer,
                              expected_modules=expected_modules)
            else:
                serve(args.config, args.task, sys.stdin.buffer, sys.stdout.buffer,
                      expected_modules=expected_modules)
        return 0
    except (HarnessError, ValueError, OSError) as error:
        print(canonical({"status": "error", "operation": "worker serve", "error": str(error)}),
              file=sys.stderr)
        return 2
