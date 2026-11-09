"""Finite historical branch effects over the original task Journal and source authority."""
from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.capabilities.process import collect_process
from kernelcat_executor.capabilities.rsi_artifacts import source_files
from kernelcat_executor.contracts.harness import validate_replay
from kernelcat_executor.contracts.models import (
    Action,
    Conflict,
    InDoubt,
    Json,
    Outcome,
    Policy,
    canonical,
    digest,
)
from kernelcat_executor.contracts.rsi import validate_rsi
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.operations.loopx_guard import LoopXTurnGuard
from kernelcat_executor.operations.registry import GateDecision

from kernelcat_worker.replay import NativeReplayFiles
from kernelcat_worker.rsi_lineage import Lineage
from kernelcat_worker.rsi_records import Records
from kernelcat_worker.rsi_replay import check_request
from kernelcat_worker.rsi_rounds import RoundEffects
from kernelcat_worker.rsi_snapshot import SourceSnapshots


def branch_workspace(records: Records, initial: Path) -> Path:
    branch = records.latest("branch")
    if branch is None:
        return initial
    value = validate_replay(branch["value"], "RsiBranch")
    workspace = Path(value["native"]["workspace"])
    expected = initial.parent / (initial.name + "-" + digest(branch["record_id"])[:20])
    if workspace != expected or workspace.resolve() != workspace:
        raise Conflict("RSI branch workspace differs from its original owner path")
    return workspace


class BranchGuard:
    name = "rsi-active-workspace"

    def __init__(self, records: Records, paths: list[str]) -> None:
        self.records, self.paths = records, frozenset(paths)
        branch = records.latest("branch")
        self.identity = branch["record_id"] if branch else None

    def check(self, action: Action, scope: str, policy: Policy) -> GateDecision:
        branch = self.records.latest("branch")
        if self.identity != (branch["record_id"] if branch else None):
            return GateDecision(verdict="deny", reason="workspace binding is frozen")
        if branch is None or action.tool not in {"write_file", "edit_file", "run_experiment"}:
            return GateDecision(verdict="abstain")
        if action.id.startswith(action.task + "/seed/"):
            return GateDecision(verdict="abstain")
        entered = False
        read = set()
        for identity, row in self.records.store.snapshot()["actions"].items():
            entered = entered or identity == self.identity
            original = row["action"]
            if (entered and original["task"] == self.records.task_id and original["tool"] == "read_file"
                    and row.get("terminal") and row["outcome"]["status"] == "completed"):
                read.add(original["arguments"]["path"])
        if not self.paths.issubset(read):
            return GateDecision(verdict="deny", reason="read the current source files before editing or measuring them")
        return GateDecision(verdict="abstain")


class BranchEffects:
    """The caller holds the original task lock and has disposed Pi before applying.

    O(E + source bytes + session bytes), bounded by the existing private-record and
    checkpoint limits. A branch is not a measurement node or another budget ledger.
    """

    def __init__(self, gateway: Gateway, records: Records, workspace: Workspace,
                 state: Path, initial_workspace: Path, source_paths: list[str],
                 fork_argv: tuple[str, str], guard: LoopXTurnGuard | None) -> None:
        self.gateway, self.records, self.workspace = gateway, records, workspace
        self.state, self.initial_workspace = state, initial_workspace
        self.source_paths, self.fork_argv, self.guard = source_paths, fork_argv, guard

    def _root(self, identity: str) -> Path:
        return self.state / "rsi-branches" / digest(self.records.task_id) / digest(identity)

    def _capability(self, workspace: Path, checkpoint: Json) -> Json:
        replay = NativeReplayFiles(self.records.store, self.state, workspace, self.records.task_id)
        return {"schema_version": "kernelcat.pi-replay/v1", "directory": str(replay.directory),
                "workspace": str(workspace), "checkpoint": replay.checkpoint(checkpoint)}

    def _target(self, request: Json, latest: Json, current: Json) -> tuple[Json, list[Json], Json]:
        if request["mode"] == "reset_context":
            if request["target_node_id"] is not None:
                raise Conflict("context reset cannot select a historical program")
            return self.records.baseline(), source_files(self.workspace, self.source_paths), current
        if request["target_node_id"] is None:
            raise Conflict("historical branch requires a target node identity")
        node_record = self.records.read(request["target_node_id"])
        if node_record["kind"] != "node":
            raise Conflict("historical branch requires a measured node")
        Lineage(self.records).check_node(node_record)
        node = node_record["value"]
        if not node["accepted"] or node["correct"] is not True or "source_commit" not in node:
            raise Conflict("historical branch requires an adopted correct source commit")
        for row in self.records.entries():
            record = self.records.decode(row)
            if record["kind"] == "invalidation" and record["value"]["measurement_id"] == node["measurement_id"]:
                raise Conflict("historical branch measurement requires review")
        accepted = self.records.read(node["acceptance_id"])
        step = int(node["boundary"]["turn_id"].rsplit("/", 1)[1])
        end = self.records.store.snapshot()["actions"][f"{self.records.task_id}/step/{step}/end"]
        if step > latest["step"]:
            raise Conflict("historical branch target follows the current checkpoint")
        workspace = self.initial_workspace
        for row in self.records.entries():
            record = self.records.decode(row)
            if record["sequence"] >= accepted["sequence"]:
                break
            if record["kind"] == "branch":
                workspace = Path(record["value"]["native"]["workspace"])
        return accepted, accepted["value"]["source"], self._capability(workspace, end["outcome"]["data"]["native_checkpoint"])

    def _admission(self, request: Json, latest: Json | None, *, reconciling: str | None = None
                   ) -> tuple[Json, list[Json], Json, Json]:
        actions = self.records.store.snapshot()["actions"]
        RoundEffects(self.records).require_settled({key: row for key, row in actions.items() if key != reconciling})
        if latest is None or "native_checkpoint" not in latest:
            raise Conflict("branch requires a settled native checkpoint")
        branch = self.records.latest("branch")
        if request["expected_branch_id"] != (branch["record_id"] if branch else None):
            raise Conflict("stale RSI branch identity")
        if request["expected_sequence"] != len(self.records.entries()):
            raise Conflict("stale RSI branch record sequence")
        decision = self.records.read(request["decision_id"])
        self._decision(request, decision)
        current = self._capability(self.workspace.root, latest["native_checkpoint"])
        baseline, files, origin = self._target(request, latest, current)
        if {entry["path"] for entry in files} != set(self.source_paths):
            raise Conflict("branch source differs from the configured source paths")
        if len(canonical([origin, current]).encode()) > 480 * 1024:
            raise Conflict("branch checkpoint metadata exceeds its private record bound")
        return baseline, files, origin, current

    def _decision(self, request: Json, decision: Json) -> None:
        if decision["kind"] == "replay_request":
            original = check_request(self.records, decision)
            if (decision != self.records.latest("replay_request")
                    or request["mode"] != original["arguments"]["mode"]
                    or request["target_node_id"] != original["arguments"]["target_node_id"]):
                raise Conflict("branch differs from the latest original Agent replay request")
            return
        # Retain reconciliation of admitted historical policy operations. New
        # production selection uses only original Agent replay requests.
        if decision != self.records.latest("intervention") or decision["kind"] != "intervention":
            raise Conflict("branch requires its latest original decision")
        allowed = {"reset_context"} if decision["value"]["action"] == "reset_context" else {"replay", "reset_context"}
        if decision["value"]["action"] not in {"reset_context", "replay_candidate"} or request["mode"] not in allowed:
            raise Conflict("intervention does not admit this branch operation")

    def reconcile_pending(self, latest: Json | None) -> None:
        """Settle a retained complete effect; incomplete work is never redispatched."""
        pending = [row for row in self.records.store.snapshot()["actions"].values()
                   if row["action"]["task"] == self.records.task_id
                   and row["action"]["tool"] == "__rsi_branch__" and not row.get("terminal")]
        if len(pending) > 1:
            raise InDoubt("multiple original branch operations require inspection")
        for row in pending:
            action = Action.model_validate_json(canonical(row["action"]))
            request = validate_rsi(action.arguments, "RsiBranchRequest")
            if action.id != request["decision_id"] + "/branch":
                raise Conflict("branch intent differs from its decision identity")
            baseline, files, origin, current = self._admission(request, latest, reconciling=action.id)
            assert latest is not None
            record = self._completed(action.id)
            self._verify_completed(record, request, baseline, files, origin, current, latest["step"] + 1)
            self.records.store.settle(action, self.records.outcome(record), 0)

    def _completed(self, identity: str) -> Json:
        path = self._root(identity) / "complete.json"
        if path.parent.resolve() != path.parent:
            raise Conflict("branch completion root changed")
        try:
            with open(path, "rb", opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK)) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 512 * 1024:
                    raise Conflict("branch completion must be a bounded regular file")
                raw = stream.read(512 * 1024 + 1)
        except FileNotFoundError as error:
            raise InDoubt("branch has no complete original effect; do not repeat: " + identity) from error
        if len(raw) > 512 * 1024:
            raise Conflict("branch completion exceeds its byte bound")
        return validate_rsi(json.loads(raw), "PrivateRecord")

    def _verify_completed(self, record: Json, request: Json, baseline: Json, files: list[Json],
                          origin: Json, current: Json, start_step: int) -> None:
        identity = request["decision_id"] + "/branch"
        value = validate_replay(record["value"], "RsiBranch")
        workspace = self.initial_workspace.parent / (self.initial_workspace.name + "-" + digest(identity)[:20])
        snapshot = value["source_commit"]
        expected = {"schema_version": "kernelcat.rsi-branch/v1", "decision_id": request["decision_id"],
            "mode": request["mode"], "target_node_id": request["target_node_id"],
            "previous_branch_id": request["expected_branch_id"], "previous_workspace": str(self.workspace.root),
            "baseline_record_id": baseline["record_id"], "start_step": start_step,
            "source_commit": snapshot, "origin": origin, "latest": current, "native": value["native"]}
        if record != {"record_id": identity, "sequence": request["expected_sequence"] + 1,
                      "kind": "branch", "value": expected}:
            raise Conflict("branch completion differs from its original intent")
        if (snapshot["repository"] != str(self._root(identity) / "sources.git")
                or snapshot["worktree"] != str(workspace) or snapshot["source_digest"] != digest(files)):
            raise Conflict("branch source differs from its admitted snapshot")
        if request["target_node_id"] is not None:
            target = self.records.read(request["target_node_id"])["value"]["source_commit"]
            if snapshot["commit"] != target["commit"]:
                raise Conflict("branch commit differs from its measured target")
        SourceSnapshots(self._root(identity)).verify_branch(snapshot, files)
        native = self._capability(workspace, value["native"]["checkpoint"])
        if native != value["native"] or native["checkpoint"]["session_id"] != str(uuid5(NAMESPACE_URL, identity)):
            raise Conflict("branch checkpoint differs from its admitted native fork")
        for path in [self.workspace.root, *self.workspace.root.rglob("*")]:
            if not path.is_symlink() and path.stat().st_mode & 0o222:
                raise Conflict("branch completion has an unfrozen original workspace")

    def apply(self, value: Json, latest: Json | None) -> Json:
        request = validate_rsi(value, "RsiBranchRequest")
        identity = request["decision_id"] + "/branch"
        action = Action(id=identity, task=self.records.task_id, tool="__rsi_branch__", arguments=request)
        previous = self.records.store.snapshot()["actions"].get(identity)
        if previous is not None:
            if previous["action"] != action.model_dump(mode="json"):
                raise Conflict("branch identity differs from its original intent")
            if not previous.get("terminal"):
                self.reconcile_pending(latest)
            return self.records.read(identity)
        baseline, files, origin, current = self._admission(request, latest)
        assert latest is not None
        workspace = self.initial_workspace.parent / (self.initial_workspace.name + "-" + digest(identity)[:20])
        if self.guard is not None:
            scope = replace(self.guard, workspace=workspace)
            for path in self.source_paths:
                check = Action(id=identity + "/scope", task=self.records.task_id, tool="write_file", arguments={"path": path})
                if scope.check(check, self.gateway.scope, self.gateway.store.policy()).verdict == "deny":
                    raise Conflict("LoopX write scope does not admit the branch workspace")
        def effect() -> Outcome:
            record = self._create(identity, request, workspace, baseline, files, origin, current, latest["step"] + 1)
            return self.records.outcome(record)
        return self.records.settled(self.gateway.internal(action, 0, effect))

    def _create(self, identity: str, request: Json, workspace: Path, baseline: Json,
                files: list[Json], origin: Json, current: Json, start_step: int) -> Json:
        root = self._root(identity)
        snapshot = SourceSnapshots(root).branch(files, workspace)
        if request["target_node_id"] is not None:
            expected = self.records.read(request["target_node_id"])["value"]["source_commit"]
            if snapshot["commit"] != expected["commit"] or snapshot["source_digest"] != expected["source_digest"]:
                raise Conflict("restored Git commit differs from its measured source")
        directory = self.state / "pi-sessions" / digest([self.records.task_id, str(workspace)])
        fork = {"source": origin, "latest": current, "directory": str(directory), "workspace": str(workspace),
                "session_id": str(uuid5(NAMESPACE_URL, identity)), "reset_context": True}
        result = collect_process(self.fork_argv, root, {"PATH": str(Path(self.fork_argv[0]).parent),
            "HOME": str(root), "LANG": "C.UTF-8"}, canonical(fork).encode(), 30, 65536, 65536)
        if result.reason or result.returncode != 0:
            raise InDoubt("native branch fork requires original-effect reconciliation: " + identity)
        native = self._capability(workspace, json.loads(result.stdout))
        record = validate_rsi({"record_id": identity, "sequence": request["expected_sequence"] + 1,
            "kind": "branch", "value": validate_replay({"schema_version": "kernelcat.rsi-branch/v1",
                "decision_id": request["decision_id"], "mode": request["mode"], "target_node_id": request["target_node_id"],
                "previous_branch_id": request["expected_branch_id"], "previous_workspace": str(self.workspace.root),
                "baseline_record_id": baseline["record_id"], "start_step": start_step, "source_commit": snapshot,
                "origin": origin, "latest": current, "native": native}, "RsiBranch")}, "PrivateRecord")
        self._freeze()
        with open(root / "complete.json", "x", opener=lambda path, flags: os.open(path, flags, 0o400)) as stream:
            stream.write(canonical(record))
            stream.flush()
            os.fsync(stream.fileno())
        return record

    def _freeze(self) -> None:
        for root, directories, files in os.walk(self.workspace.root, followlinks=False):
            for name in files:
                path = Path(root) / name
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode & ~0o222)
            for name in directories:
                path = Path(root) / name
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode & ~0o222)
        self.workspace.root.chmod(self.workspace.root.stat().st_mode & ~0o222)
