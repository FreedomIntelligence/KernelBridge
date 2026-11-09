"""One explicitly requested LoopX segment around Pi. No timer or Agent loop.

LoopX owns task routing. KernelCat owns the execution ledger. This adapter never
accepts a Todo, certifies a kernel, spends automatic quota or calls a model itself.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from kernelcat_executor.capabilities.loopx import LoopXClient
from kernelcat_executor.contracts.agent import AgentOptions
from kernelcat_executor.contracts.loopx import LoopXBinding
from kernelcat_executor.contracts.models import (
    Action,
    Conflict,
    InDoubt,
    Json,
    Outcome,
    Task,
    digest,
)
from kernelcat_executor.operations.control import SessionControl
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.profiles.store import AgentJournal

from kernelcat_worker.bindings import build
from kernelcat_worker.configuration import Config, load_config
from kernelcat_worker.loopx_driver import bind_driver
from kernelcat_worker.startup import PythonStartup, admit_config


def _record(gateway: Gateway, task: Task, identity: str, kind: str, arguments: Json,
            operation: Callable[[], Json], parents: tuple[str, ...] = ()) -> Json:
    action = Action(id=identity, task=task.id, tool=kind, arguments=arguments, parents=parents)
    outcome = gateway.internal(action, 0, lambda: Outcome(data=operation()))
    if outcome.status != "completed":
        raise InDoubt(f"LoopX operation requires reconciliation: {identity}")
    return outcome.data


def _route(binding: LoopXBinding) -> Json:
    return {"goal_id": binding.goal_id, "agent_id": binding.agent_id,
            "todo_id": binding.todo_id, "segment_id": binding.segment_id,
            "automatic_quota_spent": False, "todo_completed": False}


def _binding_data(task: Task, binding: LoopXBinding, config_digest: str) -> Json:
    data = binding.model_dump(mode="json", exclude={"segment_id"})
    data.update(task_sha256=digest(task), config_sha256=config_digest)
    return data


def _require_settled(state: Json, task: Task) -> None:
    for item in state["actions"].values():
        if item["action"]["task"] == task.id and not item.get("terminal"):
            raise InDoubt(item["action"]["id"])


def _require_writeback(state: Json, task: Task, binding: LoopXBinding,
                       config_digest: str) -> None:
    if task.id not in state["tasks"]:
        raise Conflict("LoopX writeback requires an existing registered task")
    if state["tasks"][task.id] != task.model_dump(mode="json"):
        raise Conflict("LoopX writeback task differs from the registered task")
    binding_id = task.id + "/loopx/binding"
    result_id = task.id + "/loopx/" + binding.segment_id + "/result"
    if binding_id not in state["actions"] or result_id not in state["actions"]:
        raise Conflict("no recorded LoopX segment result; writeback-only cannot execute Pi")
    recorded = state["actions"][binding_id]
    if recorded["action"]["arguments"] != _binding_data(task, binding, config_digest):
        raise Conflict("LoopX writeback configuration differs from the recorded binding")
    for identity in (binding_id, result_id):
        record = state["actions"][identity]
        if not record.get("terminal") or record["outcome"]["status"] != "completed":
            raise InDoubt(identity)


def run_segment(gateway: Gateway, task: Task, binding: LoopXBinding, client: LoopXClient,
                config_digest: str, run_pi: Callable[[], Json] | None) -> Json:
    """The caller holds an outer segment lease; Pi retains its own execution lease.

    CLI mutations have write-ahead identity/start/settlement in the existing Journal.
    A crash with unknown CLI effects is not silently retried. Known writebacks replay.
    A new segment ID is the operator's explicit request for another bounded segment.
    """
    if task.id != binding.task_id or task.scope != gateway.scope:
        raise Conflict("LoopX binding differs from the frozen KernelCat task")
    state = gateway.store.snapshot()
    _require_settled(state, task)
    if run_pi is None:
        _require_writeback(state, task, binding, config_digest)
    gateway.store.register_task(task.model_dump(mode="json"))
    bind_driver(gateway, task, "segment")
    root = task.id + "/loopx/" + binding.segment_id
    binding_id = task.id + "/loopx/binding"
    binding_data = _binding_data(task, binding, config_digest)
    result_id = root + "/result"
    _record(gateway, task, binding_id, "__loopx_binding__", binding_data,
            lambda: {"binding_sha256": digest(binding_data)})
    cached = gateway.store.snapshot()["actions"].get(result_id)
    if cached is None:
        if run_pi is None:
            raise Conflict("no recorded LoopX segment result")
        probe = client.probe()
        if probe["allowed"] is not True:
            return {"status": "loopx_blocked", "pi_started": False, **_route(binding)}
        claim_id = root + "/claim"
        _record(gateway, task, claim_id, "__loopx_claim__", {"binding": binding_id},
                lambda: {"cli_result_sha256": digest(client.claim())}, (binding_id,))
        # A durable historical claim is not proof of current ownership or quota.
        client.assert_owner()
        if client.probe()["allowed"] is not True:
            return {"status": "loopx_blocked", "pi_started": False, **_route(binding)}
        # A distinct segment is explicit continuation, not a replay of an old
        # terminal Pi checkpoint. Stable input identity survives a crash between
        # Pi completion and the segment-result write without new inference.
        SessionControl(gateway, AgentOptions()).enqueue(task.id,
            "Continue the existing frozen task for this explicitly requested LoopX segment. "
            "Inspect recorded evidence before further work. Stop at a coherent result or blocker.",
            "prompt", root + "/continuation")
        result = run_pi()  # Exactly one call to the sole Pi-backed execution handle.
        status = result.get("status")
        if not isinstance(status, str) or not status or len(status) > 80:
            raise Conflict("Pi returned an invalid segment status")
        records = {key: value for key, value in gateway.store.snapshot()["actions"].items()
                   if value["action"]["task"] == task.id
                   and not value["action"]["tool"].startswith("__loopx_")}
        result_data = {"status": status, "result_sha256": digest(result),
                       "execution_receipts_sha256": digest(records)}
        _record(gateway, task, result_id, "__loopx_result__", {"binding": binding_id},
                lambda: result_data, (claim_id,))
    result_record = gateway.store.snapshot()["actions"][result_id]
    if not result_record.get("terminal") or result_record["outcome"]["status"] != "completed":
        raise InDoubt(result_id)
    evidence = "kernelcat-receipt-sha256:" + digest(result_record)
    report_id = root + "/report"
    _record(gateway, task, report_id, "__loopx_report__", {"evidence": evidence},
            lambda: {"cli_result_sha256": digest(client.report(evidence))}, (result_id,))
    _record(gateway, task, root + "/refresh", "__loopx_refresh__", {"evidence": evidence},
            lambda: {"cli_result_sha256": digest(client.refresh())}, (report_id,))
    return {"status": "recorded", "pi_status": result_record["outcome"]["data"]["status"],
            "replayed_result": cached is not None, "evidence": evidence, **_route(binding)}


def run_command(config_path: Path, task_path: Path, binding_path: Path, *,
                execute: bool = False, writeback_only: bool = False,
                startup: PythonStartup | None = None) -> Json:
    config = load_config(config_path)
    startup = admit_config(config) if startup is None else startup
    config = startup.bind_config(config)
    task = Task.model_validate_json(task_path.read_text(encoding="utf-8"))
    binding = LoopXBinding.model_validate_json(binding_path.read_text(encoding="utf-8"))
    if task.id != binding.task_id:
        raise Conflict("LoopX binding task_id does not match --task")
    base = config_path.resolve().parent
    _check_paths(config, base, binding, binding_path)
    client = LoopXClient(binding)
    if not execute and not writeback_only:
        probe = client.probe()
        return {"status": "loopx_ready" if probe["allowed"] else "loopx_blocked",
                "read_only": True, "pi_started": False, **_route(binding)}
    if execute and writeback_only:
        raise ValueError("choose execute or writeback-only, not both")
    if writeback_only:
        # No credentials, installed Pi dependencies or agent construction needed.
        store = AgentJournal((base / config.state_dir).resolve() / "events.jsonl")
        store.snapshot()  # Must exist, be initialized and pass integrity validation.
        with store.task_lock("loopx/" + task.id):
            state = store.snapshot()
            _require_settled(state, task)
            _require_writeback(state, task, binding, digest(config))
            if not state.get("python_module_observations"):
                raise Conflict("LoopX writeback requires a previously observed Python binding")
            store.record_python_modules(startup.expected, startup.report)
            client.verify_version()
            return run_segment(Gateway(store, [], task.scope), task, binding, client,
                               digest(config), None)
    probe = client.probe()
    if probe["allowed"] is not True:
        return {"status": "loopx_blocked", "pi_started": False, **_route(binding)}
    agent = build(config, base, task, startup=startup)
    try:
        with agent.gateway.store.task_lock("loopx/" + task.id):
            return run_segment(agent.gateway, task, binding, client, digest(config),
                               lambda: agent.run(task))
    finally:
        agent.close()


def _check_paths(config: Config, base: Path, binding: LoopXBinding, binding_path: Path) -> None:
    writable = (base / config.workspace).resolve()
    protected = [Path(getattr(binding, name)).resolve()
                 for name in ("cli", "node", "registry", "runtime_root", "home")]
    protected.append(binding_path.resolve())
    if any(path.is_relative_to(writable) for path in protected):
        raise ValueError("LoopX configuration and state must be outside model-writable workspace")
    runtime = Path(binding.runtime_root).resolve()
    if writable.is_relative_to(runtime) or (base / config.state_dir).resolve().is_relative_to(runtime):
        raise ValueError("LoopX runtime state and KernelCat workspace/Journal must be disjoint")
