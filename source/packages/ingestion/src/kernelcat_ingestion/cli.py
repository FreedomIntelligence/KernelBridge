"""Explicit source collection; inspection never opens source contents or dispatches effects."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kernelcat_executor.contracts.models import HarnessError, Json, canonical

from kernelcat_ingestion.pipeline import external_state
from kernelcat_ingestion.plans import SourcePlan
from kernelcat_ingestion.ports import IngestionCommands
from kernelcat_ingestion.snapshots import DataError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kc-data", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Explicitly initialize a new external Wiki evidence root")
    init.add_argument("--wiki-root", type=Path, required=True)
    collect = commands.add_parser("collect", help="Collect and parse an explicitly pinned source plan")
    collect.add_argument("--plan", type=Path, required=True)
    collect.add_argument("--state", type=Path, required=True)
    collect.add_argument("--wiki-root", type=Path, required=True)
    collect.add_argument("--authorize-network", action="store_true")
    inspect = commands.add_parser("inspect", help="Validate and count a source plan without source I/O")
    inspect.add_argument("--plan", type=Path, required=True)
    return parser


def _load_plan(path: Path) -> SourcePlan:
    with path.open("rb") as stream:
        payload = stream.read(1048577)
    if len(payload) > 1048576:
        raise DataError("source plan exceeds 1 MiB input limit")
    return SourcePlan.model_validate_json(payload)


def _execute(args: argparse.Namespace, commands: IngestionCommands | None) -> Json:
    if args.command == "init":
        root = external_state(args.wiki_root)
        if commands is None:
            raise DataError("initialization requires explicit worker command bindings")
        commands.initialize(root)
        return {"status": "initialized", "wiki_root": str(root)}
    plan = _load_plan(args.plan)
    if args.command == "inspect":
        kinds = sorted({source.kind for source in plan.sources})
        return {
            "status": "valid", "sources": len(plan.sources),
            "assets": sum(len(source.assets) for source in plan.sources),
            "source_kinds": {kind: sum(source.kind == kind for source in plan.sources) for kind in kinds},
            "executed": False,
        }
    if commands is None:
        raise DataError("collection requires explicit worker command bindings")
    return commands.collect(args.state, args.wiki_root, plan,
                            authorize_network=args.authorize_network)


def main(argv: list[str] | None = None, *, commands: IngestionCommands | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _execute(args, commands)
        print(canonical(result))
        return 0 if result["status"] in {"initialized", "valid", "completed"} else 2
    except (OSError, ValueError, HarnessError) as exc:
        print(canonical({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
