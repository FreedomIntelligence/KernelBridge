"""Fixed installation-selected entry, bundled without product-package imports."""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from kernelcat_knowledge.contracts.schema import SchemaError
from kernelcat_knowledge.resources.module_origins import (
    PRODUCT_MODULES,
    ModuleAdmissionError,
    admit_modules,
    preflight_modules,
    read_expected,
)


def main(argv: list[str] | None = None) -> int | None:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--expected-python-modules", type=Path, required=True)
    parser.add_argument("--startup-report", type=Path)
    parser.add_argument("mode", choices=("worker", "worker-admin", "knowledge-cli",
                                        "knowledge-mcp", "ingestion", "loopx-host"))
    parser.add_argument("application_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    required = ("kernelcat_knowledge",) if args.mode.startswith("knowledge-") else PRODUCT_MODULES
    try:
        expected = read_expected(args.expected_python_modules)
        preflight_modules(expected, required)
    except (ModuleAdmissionError, SchemaError, OSError, ValueError, RecursionError) as error:
        print(f"Python startup rejected: {error}", file=sys.stderr)
        return 2
    entries = {
        "worker": "kernelcat_worker.cli", "ingestion": "kernelcat_worker.ingestion",
        "worker-admin": "kernelcat_worker.admin",
        "loopx-host": "kernelcat_worker.loopx_host",
        "knowledge-cli": "kernelcat_knowledge.api.cli",
        "knowledge-mcp": "kernelcat_knowledge.api.mcp.server",
    }
    application_args = args.application_args
    if application_args[:1] == ["--"]:
        application_args = application_args[1:]
    try:
        # -B alone still reads caches; preflight rejects those before source loads.
        sys.dont_write_bytecode = True
        for module_name in required:
            importlib.import_module(module_name)
        admitted = admit_modules(expected, required)
        entry = importlib.import_module(entries[args.mode])
        admitted.verify_loaded()
        if args.startup_report is not None:
            admitted.export(args.startup_report)
    except (ModuleAdmissionError, SchemaError, OSError, ValueError) as error:
        print(f"Python startup rejected: {error}", file=sys.stderr)
        return 2
    sys.argv = [entries[args.mode], *application_args]
    if args.mode == "worker-admin":
        return entry.main(application_args, expected_modules=expected,
                          expected_path=args.expected_python_modules)
    return entry.main(application_args, expected_modules=expected)


if __name__ == "__main__":
    raise SystemExit(main())
