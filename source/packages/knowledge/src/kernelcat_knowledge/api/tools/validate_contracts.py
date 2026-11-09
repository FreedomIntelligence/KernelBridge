#!/usr/bin/env python3
"""Validate KernelCat Wiki V1 schemas and structured contract objects."""
from __future__ import annotations

import argparse
from pathlib import Path

from kernelcat_knowledge.cards.validation import validate_repository


def _resolve_objects(root: Path, objects: list[Path]) -> list[Path] | None:
    """Resolve repo-relative object paths independently of the caller's cwd."""
    if not objects:
        return None
    return [
        path.resolve() if path.is_absolute() else (root / path).resolve()
        for path in objects
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--object", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    errors = validate_repository(root, _resolve_objects(root, args.object))
    for error in errors:
        print(f"ERROR: {error}")
    print(f"v1_contract_errors={len(errors)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
