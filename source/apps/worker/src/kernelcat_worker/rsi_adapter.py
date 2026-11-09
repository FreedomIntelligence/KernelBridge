"""Pinned, finite domain adapter for RSI; never invokes an assistant."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.capabilities.process import CommandEvaluator, collect_process
from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
from kernelcat_executor.contracts.models import (
    Action,
    Conflict,
    InDoubt,
    Json,
    Outcome,
    canonical,
    digest,
)
from kernelcat_executor.contracts.rsi import validate_policy, validate_rsi
from kernelcat_executor.operations.gateway import Gateway

from kernelcat_worker.rsi_snapshot import SourceSnapshots


def _check_command(argv: list[str], pinned: dict[str, str], label: str) -> None:
    if argv[0] not in pinned:
        raise ValueError(label + " executable must be explicitly pinned")
    if len(argv) > 1 and argv[1] not in pinned:
        raise ValueError(label + " entrypoint must be an absolute pinned file")
    for argument in argv:
        if Path(argument).is_absolute() and argument not in pinned:
            raise ValueError(label + " absolute arguments must be pinned files")


def check_configuration(value: object, workspace: Path, state: Path) -> Json:
    config = validate_rsi(value, "RsiConfiguration")
    validate_policy(config["policy"])
    pinned = {row["path"]: row["sha256"] for row in config["adapter_files"]}
    if len(pinned) != len(config["adapter_files"]):
        raise ValueError("RSI adapter repeats a pinned file")
    _check_command(config["adapter_argv"], pinned, "RSI adapter")
    if config.get("collect_argv") is not None:
        _check_command(config["collect_argv"], pinned, "RSI collector")
    for name, expected in pinned.items():
        path = Path(name)
        if not path.is_absolute() or not path.is_file() or path.is_symlink() or path.resolve() != path:
            raise ValueError("RSI adapter pin requires an absolute regular file: " + name)
        if path.is_relative_to(workspace) or workspace.is_relative_to(path):
            raise ValueError("RSI adapter overlaps model-writable source")
        if path.is_relative_to(state) or state.is_relative_to(path):
            raise ValueError("RSI adapter overlaps writable evidence state")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("RSI adapter bytes changed: " + name)
    if state.is_relative_to(workspace) or workspace.is_relative_to(state):
        raise ValueError("RSI evidence and source roots must be disjoint")
    return config


class Adapter:
    def __init__(self, config: Json, workspace: Workspace, artifacts: RuntimeArtifacts,
                 state: Path) -> None:
        self.config, self.workspace, self.artifacts, self.state = config, workspace, artifacts, state

    def call(self, gateway: Gateway, task_id: str, identity: str, request: Json) -> Json:
        request = validate_rsi(request, "AdapterRequest")
        action = Action(id=identity, task=task_id, tool="__rsi_adapter__", arguments=request)
        outcome = gateway.internal(action, self.config["credits"], lambda: self._execute(action))
        if outcome.status == "unknown":
            raise InDoubt("RSI adapter has an unresolved original operation: " + identity)
        if outcome.status != "completed":
            raise Conflict("RSI adapter did not complete: " + identity)
        raw = self.artifacts.read(outcome.data["private_reference"])
        return validate_rsi(json.loads(raw), "AdapterResult")

    def _execute(self, action: Action) -> Outcome:
        check_configuration(self.config, self.workspace.root, self.state)
        snapshots = SourceSnapshots(self.state / "rsi-snapshots" / digest(action.id))
        try:
            snapshot = snapshots.prepare(action.arguments)
        except FileExistsError:
            return Outcome(status="unknown", data={"reason": "existing_rsi_snapshot_attempt"})
        evaluator = CommandEvaluator(tuple(self.config["adapter_argv"]), Workspace(snapshots.root / "driver"),
            {"type": "object"}, self.config["credits"], self.config["timeout"],
            {"KERNELCAT_RSI_SOURCE_MANIFEST": str(snapshots.root / "snapshot.json")},
            artifact_root=self.state / "rsi-evaluations")
        outcome = evaluator.execute(action)
        return self._retained_result(action, snapshots, snapshot, outcome)

    def _retained_result(self, action: Action, snapshots: SourceSnapshots,
                         snapshot: Json, outcome: Outcome) -> Outcome:
        if outcome.status != "completed":
            return outcome
        snapshots.verify(snapshot, action.arguments)
        result = validate_rsi(outcome.data.get("rsi"), "AdapterResult")
        field = "decomposition" if action.arguments["operation"] == "decompose" else "measurement"
        if set(result) != {field}:
            raise Conflict("RSI adapter returned the wrong operation result")
        result["source_snapshot"] = validate_rsi(snapshot, "RsiSourceSnapshot")
        reference = self.artifacts.put(canonical(result).encode(), "rsi-private")
        return Outcome(data={"private_reference": reference})

    def collect_pending(self, gateway: Gateway, task_id: str, identity: str) -> Outcome:
        """Collect one original admitted effect; never prepare or execute it again.

        Caller holds the original task lock and current runtime binding. The
        explicitly configured collector has no authority to launch a new job.
        Original Journal settlement resolves its existing reservation atomically.
        """
        check_configuration(self.config, self.workspace.root, self.state)
        prior = gateway.store.snapshot()["actions"].get(identity)
        if prior is None:
            raise Conflict("RSI collection requires an existing admission: " + identity)
        action = Action.model_validate_json(canonical(prior["action"]))
        if action.task != task_id or action.tool != "__rsi_adapter__":
            raise Conflict("RSI collection does not own this task/action: " + identity)
        if prior.get("terminal"):
            return Outcome.model_validate(prior["outcome"])
        if not prior.get("started"):
            raise Conflict("RSI collection cannot settle an unstarted action: " + identity)
        argv = self.config.get("collect_argv")
        if argv is None:
            raise Conflict("RSI adapter has no explicitly configured collector")
        started = time.monotonic()
        outcome = self._collect(action, tuple(argv))
        if outcome.status != "unknown":
            gateway.store.settle(action, outcome, (time.monotonic() - started) * 1000)
        return outcome

    def _collect(self, action: Action, argv: tuple[str, ...]) -> Outcome:
        request = validate_rsi(action.arguments, "AdapterRequest")
        snapshots = SourceSnapshots(self.state / "rsi-snapshots" / digest(action.id))
        manifest = snapshots.root / "snapshot.json"
        snapshot = snapshots.retained(request)
        response = collect_process(argv, snapshots.root / "driver",
            {"KERNELCAT_RSI_SOURCE_MANIFEST": str(manifest)}, canonical(action).encode(),
            min(self.config["timeout"], 60), 1048576, 262144)
        evidence = self.artifacts.put(response.stdout, "rsi-collection-stdout")
        self.artifacts.put(response.stderr, "rsi-collection-stderr")
        if response.reason or response.returncode != 0:
            return Outcome(status="unknown", data={"reason": response.reason or "collector_failed",
                                                    "collection_reference": evidence})
        try:
            outcome = Outcome.model_validate_json(response.stdout)
        except ValueError:
            return Outcome(status="unknown", data={"reason": "invalid_collector_response",
                                                    "collection_reference": evidence})
        return self._retained_result(action, snapshots, snapshot, outcome)
