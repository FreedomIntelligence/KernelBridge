"""Finite LoopX admission and receipts; the Node host owns Pi process lifetime."""
from __future__ import annotations

import argparse
import os
import re
import signal
import sys
from collections.abc import Callable
from contextlib import ExitStack
from importlib import import_module
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal, cast

from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
from kernelcat_executor.contracts.loopx_cycle import LoopXCycle, read_host_open
from kernelcat_executor.contracts.models import Conflict, InDoubt, Json, Task, canonical, digest
from kernelcat_executor.operations.control import SessionControl
from kernelcat_executor.operations.gateway import Gateway
from kernelcat_executor.operations.loopx_guard import LoopXTurnGuard
from kernelcat_executor.operations.session import ExecutionSession
from kernelcat_executor.profiles.store import AgentJournal

from kernelcat_worker.bindings import build, check_workspace_paths, digest_task, selected_workspace
from kernelcat_worker.compose import _active_harness, _active_transport
from kernelcat_worker.configuration import load_config
from kernelcat_worker.loopx import _record
from kernelcat_worker.loopx_driver import bind_driver
from kernelcat_worker.rsi_branches import BranchEffects
from kernelcat_worker.rsi_records import Records
from kernelcat_worker.rsi_replay import pending_request
from kernelcat_worker.startup import PythonStartup, admit_config
from kernelcat_worker.transport import serve_stream


def settled_experiment(agent: ExecutionSession, task_id: str, start_step: int) -> str | None:
    """Read a settled boundary for this admitted round; never infer or rerun it."""
    actions = agent.gateway.store.snapshot()["actions"]
    if any(item["action"]["task"] == task_id and (not item.get("terminal")
            or item["outcome"]["status"] == "unknown") for item in actions.values()):
        raise InDoubt("LoopX experiment round has unresolved original effects")
    latest = agent._control.latest(task_id)
    if latest is None:
        return None
    prefix = task_id + "/rsi/turn/"
    for item in actions.values():
        action = item["action"]
        if (action["task"] != task_id or action["tool"] != "__rsi_apply__"
                or not item.get("terminal") or item["outcome"]["status"] != "completed"):
            continue
        value = action["arguments"]["record"]["value"]
        turn = value.get("turn_id", "")
        if not turn.startswith(prefix):
            continue
        step = int(turn[len(prefix):])
        proposal = actions.get(value["experiment_id"])
        if (start_step <= step <= latest["step"] and proposal is not None
                and proposal["action"]["tool"] == "run_experiment"):
            return cast(str, value["experiment_id"])
    return None


def verify_request(request: Json, plan: LoopXCycle) -> Json:
    if version("loopx") != plan.binding.version:
        raise Conflict("host requires the pinned LoopX version")
    # Reuse the pinned authority's integrity computation, never a second schema.
    # This is a finite validation helper; it does not run an Agent or perform writes.
    authority = import_module("loopx.control_plane.turn_driver.host_candidate")
    b = plan.binding
    envelope = request.get("turn_envelope", {})
    if (request.get("schema_version") != "loopx_turn_host_request_v0"
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(request.get("turn_key", "")))
            or request.get("route") != "ready_for_host"
            or not isinstance(envelope, dict)
            or envelope.get("goal_id") != b.goal_id or envelope.get("agent_id") != b.agent_id
            or envelope.get("should_run") is not True
            or envelope.get("action", {}).get("selected_todo", {}).get("todo_id") != b.todo_id
            or envelope.get("execution_policy", {}).get("normal_delivery_allowed") is not True):
        raise Conflict("LoopX Turn does not authorize the exact bound task")
    authority.extract_turn_authority(request)
    return cast(Json, envelope)



def round_prompt(envelope: Json, experimental: bool) -> str:
    instruction = ("Make one experimental change and call run_experiment. Its independently measured result "
        "ends this round; do not finish before submitting the experiment." if experimental
        else "Stop after a coherent change or concrete blocker; independent validation follows.")
    return canonical({"loopx_turn_action": envelope["action"]["primary_action"],
        "required_reads": envelope.get("required_reads", []),
        "write_scope": envelope.get("boundary", {}).get("write_scope", []),
        "instruction": "Continue the frozen KernelCat task for this bounded Turn. "
            "Read recorded evidence first. Child answers are advisory. " + instruction})


def turn_result(plan: LoopXCycle, key: str, outcome: Json, measured: str | None) -> Json:
    status = outcome.get("status")
    experimental = plan.round_mode == "experiment"
    candidate = status in {"finished", "paused"} and (not experimental or measured is not None)
    result: Json = {"schema_version": "loopx_turn_result_v0", "turn_key": key,
        "result_kind": ("validated_completion" if plan.completion and (status == "finished" or measured is not None)
                        else "validated_progress") if candidate else "iteration_failed",
        "completed_phases": ["host_execute", "typed_result"],
        "classification": "kernelcat_candidate" if candidate else "kernelcat_empty_round" if experimental else "kernelcat_iteration_stopped",
        "summary": "KernelCat candidate receipt sha256:" + digest(outcome) +
                   "; independent postcondition validation remains authoritative.",
        "recommended_action": "Inspect independently validated evidence for the selected task.",
        "next_action": "Continue only if current LoopX authority permits further work.",
        "vision_unchanged_reason": "The bound goal and frozen task contract are unchanged."}
    if candidate:
        result.update(delivery_batch_scale="single_surface", delivery_outcome="outcome_progress",
                      path_delta_mode="unchanged")
    return result


class LoopXHost:
    """One admitted round, one original task lock, no assistant scheduling."""

    def __init__(self, plan: LoopXCycle, *, startup: PythonStartup | None = None) -> None:
        config = load_config(Path(plan.config_path))
        self.startup = admit_config(config) if startup is None else startup
        self.config = self.startup.bind_config(config)
        self.plan = plan
        self.task = Task.model_validate_json(Path(plan.task_path).read_text())
        if self.task.id != plan.binding.task_id:
            raise Conflict("host task differs from the operator binding")
        self.scopes = ExitStack()
        self.agent: ExecutionSession | None = None
        self.execution: Callable[[str, Json], Json] | None = None
        self.outcome: Json | None = None
        self.cached: Json | None = None
        self.phase: Literal["new", "opening", "ready", "settled", "branched", "finished", "closed"] = "new"
        self.branches: BranchEffects | None = None
        self.start_step = 0
        self.count = 0
        self.remaining = 0
        self.key = ""
        self.root = ""

    def open(self, params: Json) -> Json:
        if self.phase != "new":
            raise Conflict("LoopX host may admit only one original request")
        request = read_host_open(params)
        envelope = verify_request(request, self.plan)
        self.phase = "opening"
        base = Path(self.plan.config_path).parent
        check_workspace_paths(self.config, base, (base / self.config.workspace).resolve() / digest_task(self.task))
        state = (base / self.config.state_dir).resolve()
        state.mkdir(parents=True, exist_ok=True)
        store = AgentJournal(state / "events.jsonl")
        self.scopes.enter_context(store.task_lock("loopx-host/" + self.task.id))
        # Reconciliation holds the same task lock as execution and runs before
        # binding a workspace. No native/model effect is reissued here.
        with store.task_lock(self.task.id):
            workspace, guard = self._workspace(base, envelope)
            if self.config.rsi is not None and store.path.exists():
                gateway = Gateway(store, [], self.task.scope)
                branches = self._branch_effects(gateway, base, state, workspace, guard)
                branches.reconcile_pending(SessionControl(gateway, self.config.agent_options).latest(self.task.id))
            workspace, guard = self._workspace(base, envelope)
            agent = build(self.config, base, self.task, guards=(guard,), external_context=True, startup=self.startup)
            self.scopes.callback(agent.close)
            bind_driver(agent.gateway, self.task, "cycle")
        self.agent = agent
        if agent.worker_cwd != workspace:
            raise Conflict("workspace selection changed during LoopX admission")
        if self.config.rsi is not None:
            self.branches = self._branch_effects(agent.gateway, base, state, workspace, guard)
        self.execution = self.scopes.enter_context(agent.open_rpc(self.task))
        self.key = request["turn_key"]
        self.root = self.task.id + "/loopx-turn/" + self.key[7:]
        self._admit(agent, request, envelope)
        self.phase = "ready" if self.outcome is None and self.cached is None else "settled"
        return {"run_pi": self.phase == "ready", "rsi_enabled": self.config.rsi is not None and self.cached is None}

    def _workspace(self, base: Path, envelope: Json) -> tuple[Path, LoopXTurnGuard]:
        workspace = selected_workspace(self.config, base, self.task)
        guard = LoopXTurnGuard(envelope, Path(self.plan.binding.project).resolve(), workspace,
                               allow_evaluation=self.plan.allow_evaluation)
        return workspace, guard

    def _branch_effects(self, gateway: Gateway, base: Path, state: Path,
                        workspace: Path, guard: LoopXTurnGuard) -> BranchEffects:
        assert self.config.rsi is not None
        return BranchEffects(gateway,
            Records(gateway.store, RuntimeArtifacts(state / "runtime-artifacts"), self.task.id),
            Workspace(workspace), state, (base / self.config.workspace).resolve() / digest_task(self.task),
            self.config.rsi["source_paths"], (self.config.pi_worker.node,
                str((base / self.config.pi_worker.root).resolve() / "src/loopx/session-fork-cli.ts")), guard)

    def _admit(self, agent: ExecutionSession, request: Json, envelope: Json) -> None:
        latest = agent._control.latest(self.task.id)
        recorded = _record(agent.gateway, self.task, self.root + "/request", "__loopx_turn_request__",
            {"request_sha256": digest(request), "plan_sha256": digest(self.plan)},
            lambda: {"start_step": latest["step"] + 1 if latest else 0})
        self.start_step = int(recorded["start_step"])
        if self.plan.round_mode == "experiment" and agent.rsi is not None:
            agent.rsi.handle("rsi_bind_round", {"request_id": self.root + "/request"}, self.start_step)
        cached = agent.gateway.store.snapshot()["actions"].get(self.root + "/result")
        if cached:
            if not cached.get("terminal") or cached["outcome"]["status"] != "completed":
                raise Conflict("LoopX host result is unresolved")
            self.cached = cached["outcome"]["data"]["result"]
            return
        control = SessionControl(agent.gateway, self.config.agent_options)
        control.enqueue(self.task.id, round_prompt(envelope, self.plan.round_mode == "experiment"),
                        "prompt", self.root + "/input")
        used = int(latest["step"] + 1 if latest else 0) - self.start_step
        settled = self._measured(agent)
        if settled is not None:
            self.outcome = {"status": "paused", "experiment_id": settled}
        elif used >= self.plan.turn_steps:
            self.outcome = (latest.get("result") if latest else None) or {"status": "paused"}
        else:
            self.remaining = self.plan.turn_steps - used
            agent.should_stop_after_turn = self._bounded_turn

    def _measured(self, agent: ExecutionSession) -> str | None:
        return (settled_experiment(agent, self.task.id, self.start_step)
                if self.plan.round_mode == "experiment" else None)

    def _bounded_turn(self, _state: Any) -> bool:
        assert self.agent is not None
        self.count += 1
        replay_requested = self.branches is not None and pending_request(self.branches.records) is not None
        return replay_requested or self._measured(self.agent) is not None or self.count >= self.remaining

    def finish(self) -> Json:
        if self.phase not in {"settled", "branched"}:
            raise Conflict("LoopX host cannot finalize before Pi completion")
        if self.cached is not None:
            result = self.cached
        else:
            assert self.agent is not None and self.outcome is not None
            agent, outcome = self.agent, self.outcome
            completed = agent._control.latest(self.task.id)
            if outcome.get("status") == "paused" and completed and completed.get("result"):
                outcome = completed["result"]
            result = turn_result(self.plan, self.key, outcome, self._measured(agent))
            _record(agent.gateway, self.task, self.root + "/result", "__loopx_turn_candidate__",
                {"request": self.root + "/request"}, lambda: {"result": result, "pi_result": outcome,
                    "end_step": completed["step"] + 1 if completed else 0, "round_mode": self.plan.round_mode},
                (self.root + "/request",))
        self.phase = "finished"
        return result

    def handle(self, method: str, params: Json) -> Json:
        if method == "loopx_open":
            return self.open(params)
        if method == "loopx_finish":
            if params:
                raise Conflict("LoopX finalization takes no caller-authored result")
            return self.finish()
        if self.phase == "settled" and self.cached is None and self.agent is not None:
            if method == "rsi_list" and self.agent.rsi is not None:
                return self.agent.rsi.handle(method, params, 0)
            if method == "loopx_branch" and self.branches is not None:
                result = self.branches.apply(params, self.agent._control.latest(self.task.id))
                self.phase = "branched"
                return result
        if self.phase != "ready" or self.execution is None:
            raise Conflict("LoopX host has no active Pi execution")
        result = self.execution(method, params)
        if method == "complete":
            self.outcome = result
            self.phase = "settled"
        return result

    def close(self) -> None:
        self.phase = "closed"
        self.scopes.close()


def main(argv: list[str] | None = None, *, expected_modules: object | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args(argv)
    def interrupted(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        plan = LoopXCycle.model_validate_json(args.plan.read_text())
        from kernelcat_worker.loopx_cycle import validate_plan
        config, _task = validate_plan(plan, args.plan)
        startup = admit_config(config, expected_modules=expected_modules)
        _active_harness(config, Path(plan.config_path).parent, os.environ)
        config = _active_transport(config, os.environ)
        host = LoopXHost(plan, startup=startup)
        try:
            serve_stream(sys.stdin.buffer, sys.stdout.buffer, host.handle,
                         max_frame_bytes=config.pi_worker.max_frame_bytes, loopx=True)
        finally:
            host.close()
        return 0
    except Exception:
        # Private configuration must not become a public LoopX result.
        print("KernelCat host stopped; inspect local execution receipts.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
