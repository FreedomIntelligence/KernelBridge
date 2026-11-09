"""Apply one original Agent replay request after standalone Pi has stopped."""
from __future__ import annotations

from pathlib import Path

from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
from kernelcat_executor.contracts.models import Conflict, Json, Task, digest
from kernelcat_executor.contracts.rsi import validate_rsi
from kernelcat_executor.operations.control import SessionControl
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.profiles.store import AgentJournal

from kernelcat_worker.bindings import (
    bind_retained_runtime,
    check_workspace_paths,
    digest_task,
    selected_workspace,
)
from kernelcat_worker.configuration import Config, load_config
from kernelcat_worker.rsi_branches import BranchEffects
from kernelcat_worker.rsi_records import Records
from kernelcat_worker.rsi_replay import check_request, pending_request, replay_plugin
from kernelcat_worker.startup import PythonStartup


def _result(status: str) -> Json:
    return validate_rsi({"status": status, "action_id": None, "branch_id": None,
        "mode": None, "evidence": None, "replayed": False,
        "new_experiment": False}, "RsiReplayResult")


def replay_command(config_path: Path, task_path: Path, startup: PythonStartup) -> Json:
    if not config_path.is_absolute() or not task_path.is_absolute():
        raise ValueError("RSI replay requires absolute configuration and task paths")
    config = startup.bind_config(load_config(config_path))
    if replay_plugin(config.rsi) is None:
        return _result("disabled")
    assert config.rsi is not None
    base = config_path.parent
    state = (base / config.state_dir).resolve()
    store = AgentJournal(state / "events.jsonl")
    if not store.path.is_file():
        return _result("no_request")
    task = Task.model_validate_json(task_path.read_text())
    with store.task_lock("loopx-host/" + task.id), store.task_lock(task.id):
        return _replay_locked(config, base, store, task, startup)


def _replay_locked(config: Config, base: Path, store: AgentJournal,
                   task: Task, startup: PythonStartup) -> Json:
    assert config.rsi is not None
    if store.snapshot()["tasks"].get(task.id) != task.model_dump(mode="json"):
        raise Conflict("RSI replay task differs from its original registration")
    if any(row["action"]["task"] == task.id and row["action"]["tool"] in
           {"__loopx_driver__", "__loopx_binding__", "__loopx_turn_request__"}
           for row in store.snapshot()["actions"].values()):
        raise Conflict("LoopX-bound replay requires its original LoopX authority")
    workspace = selected_workspace(config, base, task)
    check_workspace_paths(config, base, workspace)
    bind_retained_runtime(config, base, store, startup)
    gateway = Gateway(store, [], task.scope)
    state = (base / config.state_dir).resolve()
    records = Records(store, RuntimeArtifacts(state / "runtime-artifacts"), task.id)
    effects = BranchEffects(gateway, records, Workspace(workspace), state,
        (base / config.workspace).resolve() / digest_task(task), config.rsi["source_paths"],
        (config.pi_worker.node, str((base / config.pi_worker.root).resolve() /
                                   "src/loopx/session-fork-cli.ts")), None)
    return _restore_request(records, effects, SessionControl(gateway, config.agent_options), task)


def _restore_request(records: Records, effects: BranchEffects,
                     control: SessionControl, task: Task) -> Json:
    requested = pending_request(records)
    if requested is not None:
        request = check_request(records, requested)
        previous = records.latest("branch")
        branch = effects.apply({"decision_id": requested["record_id"],
            "mode": request["arguments"]["mode"], "target_node_id": request["arguments"]["target_node_id"],
            "expected_branch_id": previous["record_id"] if previous else None,
            "expected_sequence": len(records.entries())}, control.latest(task.id))
    else:
        retained_branch = records.latest("branch")
        if retained_branch is None:
            return _result("no_request")
        branch = retained_branch
        original = records.read(branch["value"]["decision_id"])
        if original["kind"] != "replay_request":
            return _result("no_request")
        request = check_request(records, original)
    # A lost reply after branch settlement reconciles the same input; enqueue is
    # itself idempotent in the original SessionControl/Journal.
    control.enqueue(task.id,
        "Continue the admitted task from the restored native checkpoint. Read the restored source files "
        "and original receipts before editing or measuring; preserve the original goal and user constraints.",
        "prompt", branch["record_id"] + "/continue")
    receipt = records.store.snapshot()["actions"][branch["record_id"]]
    return validate_rsi({"status": "restored", "action_id": request["action_id"],
        "branch_id": branch["record_id"], "mode": request["arguments"]["mode"],
        "evidence": "kernelcat-receipt-sha256:" + digest(receipt),
        "replayed": requested is None, "new_experiment": False}, "RsiReplayResult")
