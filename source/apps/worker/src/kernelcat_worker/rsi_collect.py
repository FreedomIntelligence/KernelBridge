"""Explicit collection of an original RSI effect, without another assistant turn."""
from __future__ import annotations

from pathlib import Path

from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
from kernelcat_executor.contracts.models import Conflict, Json, Task
from kernelcat_executor.contracts.rsi import validate_rsi
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.profiles.store import AgentJournal

from kernelcat_worker.bindings import (
    bind_retained_runtime,
    check_workspace_paths,
    selected_workspace,
)
from kernelcat_worker.configuration import load_config
from kernelcat_worker.rsi_adapter import Adapter
from kernelcat_worker.startup import PythonStartup


def collect_command(config_path: Path, task_path: Path, identity: str,
                    startup: PythonStartup) -> Json:
    """Hold the existing execution locks while collecting a retained admission."""
    if not config_path.is_absolute() or not task_path.is_absolute():
        raise ValueError("RSI collection requires absolute configuration and task paths")
    config = startup.bind_config(load_config(config_path))
    if config.rsi is None:
        raise Conflict("RSI collection requires the original adapter configuration")
    base = config_path.parent
    state = (base / config.state_dir).resolve()
    store = AgentJournal(state / "events.jsonl")
    if not store.path.is_file():
        raise Conflict("RSI collection requires an existing original Journal")
    task = Task.model_validate_json(task_path.read_text())
    with store.task_lock("loopx-host/" + task.id), store.task_lock(task.id):
        if store.snapshot()["tasks"].get(task.id) != task.model_dump(mode="json"):
            raise Conflict("RSI collection task differs from its original registration")
        workspace = selected_workspace(config, base, task)
        check_workspace_paths(config, base, workspace)
        if not workspace.is_dir():
            raise Conflict("RSI collection requires its retained workspace")
        bind_retained_runtime(config, base, store, startup)
        adapter = Adapter(config.rsi, Workspace(workspace),
                          RuntimeArtifacts(state / "runtime-artifacts"), state)
        before = store.snapshot()["actions"].get(identity)
        outcome = adapter.collect_pending(Gateway(store, [], task.scope), task.id, identity)
        return validate_rsi({"action_id": identity, "status": outcome.status,
                "replayed": before is not None and before.get("terminal", False),
                "settled": outcome.status != "unknown", "data": outcome.data,
                "new_experiment": False}, "RsiCollectionResult")
