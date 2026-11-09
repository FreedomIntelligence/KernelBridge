#!/usr/bin/env python3
"""Command-line adapter for the Knowledge trust validator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kernelcat_knowledge.cards.trust import TrustValidator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate KernelCat Trust Contract v1")
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--legacy-audit", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    validator = TrustValidator(args.repo_root, legacy_audit=args.legacy_audit)
    report = (
        validator.validate_paths([path.resolve() for path in args.paths])
        if args.paths
        else validator.validate_repository()
    )
    if args.as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        for item in report.findings:
            field_name = f" [{item.field}]" if item.field else ""
            print(f"{item.level.upper():7} {item.path}{field_name}: {item.message}")
        print(
            f"scanned={report.scanned_files} opted_in={report.opted_in_files} errors={len(report.errors)} warnings={len(report.warnings)}"
        )
    return 1 if report.errors or (args.strict and report.warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
