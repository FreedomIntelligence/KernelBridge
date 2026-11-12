"""Validate an operator plan and start the bounded Node LoopX Turn launcher."""
from __future__ import annotations

import os
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from kernelcat_executor.capabilities.loopx import LoopXClient
from kernelcat_executor.capabilities.process import collect_process
from kernelcat_executor.contracts.loopx_cycle import LoopXCycle, read_request
from kernelcat_executor.contracts.models import Conflict, Json, Task, canonical, digest
from kernelcat_executor.profiles.store import AgentJournal

from kernelcat_worker.bindings import _check_executable, _file_hashes
from kernelcat_worker.configuration import Config, load_config
from kernelcat_worker.loopx import _check_paths
from kernelcat_worker.startup import PythonStartup, admit_config, expected_input


def validator_files(plan: LoopXCycle) -> dict[str, str]:
    """Hash explicit file arguments as the validator will resolve them at its cwd."""
    project = Path(plan.binding.project).resolve()
    resolved = tuple(str((project / value).resolve()) for value in plan.validator_argv
                     if (project / value).is_file())
    return _file_hashes(resolved)


def validate_plan(plan: LoopXCycle, path: Path) -> tuple[Config, Task]:
    for value in (plan.config_path, plan.task_path):
        if not Path(value).is_absolute() or not Path(value).is_file():
            raise ValueError("cycle configuration and task must be existing absolute paths")
    config = load_config(Path(plan.config_path))
    if any(tool.name in {"online_search", "online_fetch", "online_read"} for tool in config.business_tools):
        raise ValueError("Legacy LoopX cycle does not support online research; use the application host or CANN SDK entrypoint")
    if plan.round_mode == "experiment" and (config.rsi is None or not plan.allow_evaluation):
        raise ValueError("experiment rounds require configured RSI and explicit allow_evaluation")
    task = Task.model_validate_json(Path(plan.task_path).read_text())
    if task.id != plan.binding.task_id:
        raise Conflict("cycle task does not match binding")
    base = Path(plan.config_path).parent
    _check_paths(config, base, plan.binding, path)
    _check_executable(plan.validator_argv, "independent validator")
    writable = (base / config.workspace).resolve()
    for value in (*validator_files(plan), plan.config_path, plan.task_path):
        candidate = Path(value).resolve()
        if candidate.is_relative_to(writable):
            raise ValueError("cycle configuration and validator must be outside writable candidates")
    return config, task


def _launch_inputs(config: Config, base: Path, startup: PythonStartup,
                   expected_path: Path | None) -> tuple[Path, Path, Path, Path]:
    if expected_path is None:
        raise ValueError("LoopX cycle requires an explicit expected Python modules file")
    expected_input(expected_path, startup.expected)
    binding = expected_path.resolve(strict=True)
    root = (base / config.pi_worker.root).resolve()
    writable = (base / config.workspace).resolve()
    if root.is_relative_to(writable) or writable.is_relative_to(root):
        raise ValueError("LoopX launcher and writable workspace must be disjoint")
    script = root / "src/replay/loopx-runner.mjs"
    bootstrap = root / "python/generated_startup.py"
    host = root / "src/loopx/host-cli.ts"
    for label, selected in (("expected binding", binding), ("launcher", script),
                            ("Python bootstrap", bootstrap), ("Node host", host)):
        if not selected.is_file():
            raise ValueError(f"LoopX {label} must be an existing file")
        if selected.resolve().is_relative_to(writable):
            raise ValueError(f"LoopX {label} must be outside the writable workspace")
    return script, bootstrap, binding, host


def _environment(client: LoopXClient, config: Config, import_roots: tuple[Path, ...],
                 execute: bool) -> dict[str, str]:
    environment = dict(client.environment)
    # Only configured key names cross this boundary, never payloads or cursors.
    if execute:
        for provider in config.providers.values():
            key = os.environ.get(provider.api_key_env, "")
            if not key:
                raise ValueError("a configured model credential is missing")
            environment[provider.api_key_env] = key
    environment.pop("PYTHONPYCACHEPREFIX", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(map(str, import_roots))
    return environment


def run_cycle_command(path: Path, *, execute: bool = False,
                      startup: PythonStartup | None = None,
                      expected_path: Path | None = None) -> Json:
    path = path.resolve()
    plan = LoopXCycle.model_validate_json(path.read_text())
    config, _task = validate_plan(plan, path)
    startup = admit_config(config) if startup is None else startup
    config = startup.bind_config(config)
    base = Path(plan.config_path).parent
    script, bootstrap, binding_file, host = _launch_inputs(config, base, startup, expected_path)
    import_roots = startup.import_roots
    client = LoopXClient(plan.binding)
    client.verify_version()
    state = (base / config.state_dir).resolve()
    cursor = state / "loopx-cycles" / (digest({"goal": plan.binding.goal_id,
        "agent": plan.binding.agent_id, "todo": plan.binding.todo_id,
        "cycle": plan.binding.segment_id}) + ".json")
    payload = {"goalId": plan.binding.goal_id, "agentId": plan.binding.agent_id,
        "todoId": plan.binding.todo_id, "cycleId": plan.binding.segment_id,
        "project": plan.binding.project, "cursor": str(cursor), "execute": execute,
        "maxTurns": plan.max_turns, "maxSeconds": plan.max_seconds,
        "turnTimeout": plan.turn_timeout, "validatorTimeout": plan.validator_timeout,
        "cliPrefix": [plan.binding.cli, "--registry", plan.binding.registry,
                      "--runtime-root", plan.binding.runtime_root, "--format", "json"],
        "hostArgv": [config.pi_worker.node, str(host), "--python", sys.executable,
                     "--expected-python-modules", str(binding_file),
                     "--plan", str(path), "--timeout-ms", str(int(min(config.pi_worker.timeout, plan.turn_timeout) * 1000)),
                     "--max-frame-bytes", str(config.pi_worker.max_frame_bytes)],
        "validatorArgv": list(plan.validator_argv),
        "planDigest": digest({"plan": plan.model_dump(mode="json"),
                              "config": config.model_dump(mode="json"),
                              "task": _task.model_dump(mode="json"),
                              "validator_files": validator_files(plan),
                              "launcher": script.read_text(),
                              "node_host": _file_hashes((str(host),)),
                              "python_bootstrap": _file_hashes((str(bootstrap),))})}
    env = _environment(client, config, import_roots, execute)
    if execute:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = AgentJournal(state / "events.jsonl")
    lease: Any = store.task_lock("loopx-cycle/" + plan.binding.task_id) if execute else nullcontext()
    with lease:
        process = collect_process((plan.binding.node, str(script)), Path(plan.binding.project),
            env, canonical(payload).encode(), plan.max_seconds + 15, 65536, 65536)
    if process.reason is not None:
        raise ValueError("cycle interrupted or exceeded its bound; inspect saved Turn keys")
    result = read_request(process.stdout)
    if "status" not in result:
        raise ValueError("cycle returned no status")
    return result
