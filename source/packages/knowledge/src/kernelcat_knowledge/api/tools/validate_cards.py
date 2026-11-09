#!/usr/bin/env python3
"""Command-line adapter for operator-card content validation."""

from __future__ import annotations

import argparse
from pathlib import Path

from kernelcat_knowledge.cards.content import load_cards, validate_cards


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate compact operator decision cards"
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--index", type=Path, default=None)
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    index = args.index.resolve() if args.index else root / "wiki/cards"
    try:
        errors = validate_cards(root, index)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    for error in errors:
        print(f"ERROR: {error}")
    card_count = len(load_cards(index))
    print(f"cards={card_count} errors={len(errors)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
