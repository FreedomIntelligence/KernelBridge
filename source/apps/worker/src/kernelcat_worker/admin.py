"""CLI adapter: one canonical JSON on stdout; failures have nonzero exit codes."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kernelcat_executor.capabilities.skills import Skills
from kernelcat_executor.contracts.models import HarnessError, Json, canonical
from kernelcat_executor.operations.requests import recorded_request
from kernelcat_executor.profiles.store import AgentJournal
from kernelcat_executor.state.journal import Journal

from kernelcat_worker.bindings import observe_versions, preflight
from kernelcat_worker.configuration import load_config
from kernelcat_worker.startup import PythonStartup, expected_input
from kernelcat_worker.versions import export_version_evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kernelcat-admin", allow_abbrev=False)
    startup = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    startup.add_argument("--expected-python-modules", type=Path)
    startup.add_argument("--startup-report", type=Path, help="create-only current reader admission report")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", parents=[startup], allow_abbrev=False,
        help="offline configuration preflight; no paid calls")
    doctor.add_argument("--config", type=Path, required=True)
    versions = sub.add_parser("versions", parents=[startup], allow_abbrev=False,
        help="observe local versions or read a retained runtime binding")
    version_source = versions.add_mutually_exclusive_group(required=True)
    version_source.add_argument("--config", type=Path)
    version_source.add_argument("--journal", type=Path)
    versions.add_argument("--evidence", type=Path, help="explicit create-only probe report export")
    status = sub.add_parser("status", parents=[startup], allow_abbrev=False,
        help="validate journal integrity and inspect durable state")
    status.add_argument("--journal", type=Path, required=True)
    profiles = sub.add_parser("profiles", parents=[startup], allow_abbrev=False,
        help="inspect active and task-pinned agent revisions; read only")
    profiles.add_argument("--journal", type=Path, required=True)
    skills = sub.add_parser("skills", parents=[startup], allow_abbrev=False,
        help="list SKILL.md metadata without model calls")
    skills.add_argument("--root", action="append", type=Path, required=True)
    request_cmd = sub.add_parser("request", parents=[startup], allow_abbrev=False,
        help="reconstruct logged model request without sending it")
    request_cmd.add_argument("--journal", type=Path, required=True)
    request_cmd.add_argument("--action-id", required=True)
    loopx = sub.add_parser("loopx", parents=[startup], allow_abbrev=False,
        help="inspect or explicitly execute one LoopX-bound Pi segment")
    loopx.add_argument("--config", type=Path, required=True)
    loopx.add_argument("--task", type=Path, required=True)
    loopx.add_argument("--binding", type=Path, required=True)
    mode = loopx.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="claim, run Pi once and report evidence")
    mode.add_argument("--writeback-only", action="store_true", help="replay recorded delivery without inference")
    cycle = sub.add_parser("loopx-cycle", parents=[startup], allow_abbrev=False,
        help="preview or execute bounded official LoopX Turns with Pi")
    cycle.add_argument("--plan", type=Path, required=True)
    cycle.add_argument("--execute", action="store_true")
    collect = sub.add_parser("rsi-collect", parents=[startup], allow_abbrev=False,
        help="collect an existing admitted RSI job without starting another experiment")
    collect.add_argument("--config", type=Path, required=True)
    collect.add_argument("--task", type=Path, required=True)
    collect.add_argument("--action-id", required=True)
    replay = sub.add_parser("rsi-replay", parents=[startup], allow_abbrev=False,
        help="apply one original Agent replay request after standalone Pi has stopped")
    replay.add_argument("--config", type=Path, required=True)
    replay.add_argument("--task", type=Path, required=True)
    return parser


def _versions(args: argparse.Namespace, startup: PythonStartup) -> Json:
    if args.journal is not None:
        if args.evidence is not None:
            raise ValueError("probe evidence export requires --config")
        return {"binding": AgentJournal(args.journal).runtime_binding(), "read_only": True}
    config = startup.bind_config(load_config(args.config))
    capture = observe_versions(config, args.config.resolve().parent, startup=startup)
    if args.evidence is not None:
        export_version_evidence(args.evidence, capture)
    return dict(capture.lock)


def _execute(args: argparse.Namespace, startup: PythonStartup, expected_path: Path | None) -> Json:
    if args.command == "rsi-replay":
        from kernelcat_worker.rsi_replay_command import replay_command
        return replay_command(args.config, args.task, startup)
    if args.command == "rsi-collect":
        from kernelcat_worker.rsi_collect import collect_command
        return collect_command(args.config, args.task, args.action_id, startup)
    if args.command == "loopx-cycle":
        from kernelcat_worker.loopx_cycle import run_cycle_command
        return run_cycle_command(args.plan, execute=args.execute, startup=startup,
                                 expected_path=expected_path)
    if args.command == "loopx":
        from kernelcat_worker.loopx import run_command
        return run_command(args.config, args.task, args.binding, execute=args.execute,
                           writeback_only=args.writeback_only, startup=startup)
    if args.command == "doctor":
        config = startup.bind_config(load_config(args.config))
        return preflight(config, args.config.resolve().parent, startup=startup)
    if args.command == "versions":
        return _versions(args, startup)
    if args.command == "status":
        return AgentJournal(args.journal).snapshot()
    if args.command == "profiles":
        snapshot = AgentJournal(args.journal).snapshot()
        return {"active": snapshot["agent_profiles"], "task_pins": snapshot["agent_pins"],
                "activations": snapshot["agent_activations"], "read_only": True}
    if args.command == "request":
        request = recorded_request(Journal(args.journal), args.action_id)
        return {"request": request.model_dump(mode="json"),
                "request_digest": request.fingerprint(), "network_called": False}
    if args.command == "skills":
        return {"skills": Skills(tuple(args.root)).catalog()}
    raise ValueError(f"unsupported administration command: {args.command}")


def _exit_code(command: str, result: Json) -> int:
    if command == "rsi-collect":
        return 0 if result["status"] == "completed" else 3
    if command == "loopx-cycle":
        return 0 if result.get("status") in {"ready", "completed", "turn_limit"} else 3
    if command == "loopx":
        ready = result.get("status") == "loopx_ready"
        finished = result.get("status") == "recorded" and result.get("pi_status") == "finished"
        return 0 if ready or finished else 3
    return 0


def main(argv: list[str] | None = None, *, expected_modules: object | None = None,
         expected_path: Path | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] == "run":
        print("Python product run is retired; use apps/agent/src/main.ts.", file=sys.stderr)
        return 2
    args = _parser().parse_args(values)
    try:
        binding_path = args.expected_python_modules or expected_path
        expected = expected_input(binding_path, expected_modules)
        startup = PythonStartup.admit(expected)
        if args.startup_report is not None:
            startup.export(args.startup_report)
        result = _execute(args, startup, binding_path)
        print(canonical(result))
        return _exit_code(args.command, result)
    except KeyboardInterrupt:
        print(canonical({"error": "Interrupted", "message": "pending reservations are retained"}),
              file=sys.stderr)
        return 130
    except (HarnessError, ValueError, OSError) as error:
        print(canonical({"error": type(error).__name__, "message": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
