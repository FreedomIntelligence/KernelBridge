"""Receipt/effect RPC for Pi. No provider-selection or model/tool scheduling loop.

Pi requests one provider attempt, one finite tool batch, or one checkpoint.
This authority verifies requests against durable facts and never advances an
agent on its own. Conversational scheduling is exclusively in the Pi SDK.
"""
from __future__ import annotations

from concurrent.futures import Future
from threading import Thread
from typing import Any

from kernelcat_executor.contracts.context import (
    validate_context_source,
    validate_model_context_request,
    validate_submission,
)
from kernelcat_executor.contracts.harness import (
    NativeReplay,
    native_name,
    validate_harness,
    validate_harness_request,
    validate_native_arguments,
    validate_native_begin,
    validate_native_call,
    validate_native_settle,
    validate_replay,
)
from kernelcat_executor.contracts.messages import TURN_SCHEMA, AssistantTurn, turn_actions
from kernelcat_executor.contracts.models import (
    Action,
    BudgetExceeded,
    Conflict,
    InDoubt,
    Json,
    Outcome,
    Policy,
    Task,
    canonical,
    digest,
)
from kernelcat_executor.contracts.ports import HarnessProposer, ManagedStore, ProfileStore
from kernelcat_executor.contracts.rsi import RsiSession
from kernelcat_executor.contracts.runtime import ContextAdmission
from kernelcat_executor.generated.context import ContextBudget
from kernelcat_executor.generated.harness import HarnessInput
from kernelcat_executor.operations.context import source
from kernelcat_executor.operations.delegated import DelegatedEffects
from kernelcat_executor.operations.native import NativeEffects
from kernelcat_executor.operations.recovery import ModelRetry, latest_model_attempt
from kernelcat_executor.operations.requests import propose, recorded_harness_tools
from kernelcat_executor.operations.turns import CallCancelled, execute_calls

SCOUT_TOOLS = frozenset({"read_file", "load_skill", "read_receipt", "read_learned_skill", "finish"})


class ExecutionRPC:
    """A finite, task-scoped effect protocol. The owner is a Pi process handle."""
    def __init__(self, owner: Any, task: Task, *, retry: ModelRetry | None = None) -> None:
        self.owner, self.gateway, self.task = owner, owner.gateway, task
        self.store, self.retry = self.gateway.store, retry
        self.initialized = False
        self.prepared = False
        self.active = False
        self.step = 0
        self.prior: str | None = None
        self.result: Json | None = None
        self.last_result: Json | None = None
        self.agent: Json | None = None
        self.context: Json | None = None
        self.context_source: Json | None = None
        self.context_admission: ContextAdmission | None = None
        self.route_index = 0
        self.failed: list[str] = []
        self.proposal_id = ""
        self.text = ""
        self.calls: tuple[Action, ...] = ()
        self.outcomes: list[Outcome] = []
        self.plan: Any = None
        self.specs = tuple(s for s in self.gateway.specifications()
                           if owner.role != "scout" or s.name in SCOUT_TOOLS)
        self.allowed = {s.name for s in self.specs}
        self.remote: Any = None
        self.remote_thread: Thread | None = None
        self.remote_result: Future[Outcome] | None = None
        self.remote_action: Action | None = None
        self.child: DelegatedEffects | None = None
        self.harness: HarnessInput | None = None
        self.harness_tools: dict[str, Json] = {}
        self.native_leaves = frozenset(self.allowed) - {"delegate", "finish"}
        self.native_invocations: dict[str, NativeEffects] = {}
        self.replay: NativeReplay | None = None

    def _unresolved(self) -> None:
        for item in self.store.snapshot()["actions"].values():
            if item["action"]["task"] == self.task.id and not item.get("terminal"):
                raise InDoubt(f"unresolved action {item['action']['id']}")

    def hello(self, params: Json | None = None) -> Json:
        if self.initialized:
            raise Conflict("duplicate worker handshake")
        request = validate_replay({} if params is None else params, "HelloRequest")
        self._unresolved()
        latest = self.owner._control.latest(self.task.id)
        hello = self._hello_capabilities(request, latest)
        if self.owner.profile is not None:
            if not isinstance(self.store, ProfileStore):
                raise Conflict("versioned profiles require a ProfileStore")
            self.store.bind_profile(self.task.scope, self.owner.profile)
            self.agent = self.store.pin_profile(self.task.id).projection()
        if self.owner._cancelled():
            self.result = {"status": "cancelled", "step": 0}
        else:
            if self.owner.role == "scout" and self.task.seed_files:
                raise Conflict("read-only scout cannot seed files")
            for path, text in self.task.seed_files.items():
                outcome = self.gateway.execute(Action(id=self.task.id + "/seed/" + digest(path),
                    task=self.task.id, tool="write_file",
                    arguments={"path": path, "text": text, "expected_sha256": None}))
                if outcome.status == "unknown":
                    raise InDoubt("unresolved seed")
                if outcome.status != "completed":
                    self.result = {"status": "seed_failed", "result": outcome.model_dump(mode="json")}
                    break
        if latest:
            self.step, self.prior, self.last_result = latest["step"] + 1, latest["prior"], latest["result"]
        self.initialized = True
        return hello

    def _hello_capabilities(self, request: Json, latest: Json | None) -> Json:
        hello: Json = {"task_id": self.task.id, "goal": self.task.goal,
                       "tools": [s.model_dump(mode="json") for s in self.specs],
                       "execution_mode": self.owner.options.tool_execution}
        if self.owner.external_context and all(
                isinstance(proposer, HarnessProposer) for proposer in self.owner.proposers.values()):
            native_tools = ["read", "ls", "find", "grep"]
            if self.owner.role != "scout":
                native_tools.extend(["write", "edit", "bash"])
            if self.owner.workspace_policy.get("codemode"):
                native_tools.extend(["codemode", "tool_search"])
            hello["native_harness"] = {"schema_version": "kernelcat.pi-harness/v1",
                "adapter": "pi@1.5.0", "native_tools": native_tools,
                "mcp_server": "kernelcat", "timeout": self.owner.worker_timeout,
                "workspace": self.owner.workspace_policy}
        if self.owner.rsi is not None:
            if "native_harness" not in hello:
                raise Conflict("RSI requires the native Pi harness and external context")
            hello["rsi"] = {"schema_version": "kernelcat.rsi/v1"}
        replay = self.owner.replay if request.get("native_replay") and "native_harness" in hello else None
        if replay is not None:
            hello["replay"] = replay.start(latest)
        elif ((latest is not None and "native_checkpoint" in latest)
              or self.owner._control.native_checkpoint_required(self.task.id)):
            raise Conflict("native checkpoint continuation requires a replay-capable Pi client")
        self.replay = replay
        return hello

    def prepare_turn(self) -> Json:
        if self.active or self.prepared:
            raise Conflict("previous turn is not settled")
        self._unresolved()
        if self.owner._cancelled():
            self.result = {"status": "cancelled", "step": self.step}
        if self.result is not None:
            return {"stop": True, "result": self.result}
        state = self.store.snapshot()
        policy = Policy.model_validate_json(canonical(state["policy"]))
        if self.step >= policy.max_steps:
            self.result = self.last_result or {"status": "step_limit", "steps": self.step}
            return {"stop": True, "result": self.result}
        inputs = self.owner._control.take(self.task.id, self.step, terminal=self.last_result is not None)
        if self.last_result is not None and not inputs:
            self.result = self.last_result
            return {"stop": True, "result": self.result}
        self.owner._emit("turn_start", {}, self.step)
        for message in inputs:
            self.owner._message({"role": "user", "text": message["text"],
                "input_id": message["id"], "kind": message["kind"]}, self.step)
        self.context, self.route_index, self.failed = None, 0, []
        self.harness = None
        self.harness_tools = {}
        self.prepared = True
        prepared: Json = {"routes": list(policy.routes), "step": self.step}
        if self.owner.external_context:
            self.context_source = source(self.task, policy, self.store.snapshot(),
                                         self.owner.skills, agent=self.agent)
            if isinstance(self.store, ManagedStore):
                self.context_source["runtime"] = self.store.runtime_state()["stamp"]
            if self.owner.rsi is not None:
                self.context_source["rsi"] = self.owner.rsi.context(self.step)
            prepared["context_source"] = validate_context_source(self.context_source)
        return prepared

    def _submitted_context(self, params: Json, policy: Policy) -> ContextBudget:
        if "harness" in params:
            native = validate_harness_request(params)
            checked_value = {"route": native["route"], "context": native["context"]}
            if "commit" in native:
                checked_value["commit"] = native["commit"]
            checked = validate_model_context_request(checked_value)
        else:
            checked = validate_model_context_request(params)
        if self.context_source is None:
            raise Conflict("model request has no admitted context source")
        if canonical(policy) != canonical(self.context_source["policy"]):
            raise Conflict("live policy changed after context admission")
        submitted = validate_submission(self.context_source, dict(checked["context"]), policy)
        if self.context is not None and canonical(self.context) != canonical(submitted):
            raise Conflict("provider fallback changed the admitted context")
        self.context = submitted
        self.harness = None if "harness" not in params else validate_harness(
            params["harness"], submitted, self.specs, policy.context_bytes)
        self.context_admission = None
        if isinstance(self.store, ManagedStore):
            if "commit" not in checked:
                raise Conflict("managed parent model requires ContextCommit")
            self.context_admission = ContextAdmission(checked["commit"],
                validate_context_source(self.context_source), checked["context"],
                dict(self.harness) if self.harness is not None else submitted)
        elif "commit" in checked:
            raise Conflict("legacy session cannot adopt a managed ContextCommit")
        return checked["context"]["budget"]

    def model(self, params: Json) -> Json:
        expected = {"route", "context"} if self.owner.external_context else {"route"}
        if isinstance(self.store, ManagedStore):
            expected = expected | {"commit"}
        native_expected = expected | {"harness"} if self.owner.external_context else expected
        if not self.prepared or self.active or set(params) not in (expected, native_expected):
            raise Conflict("model attempt outside a prepared turn")
        self._unresolved()
        if self.owner._cancelled():
            self.result = {"status": "cancelled", "step": self.step}
            self.prepared = False
            return {"stop": True, "result": self.result}
        state = self.store.snapshot()
        policy = Policy.model_validate_json(canonical(state["policy"]))
        expected_budget = self._submitted_context(params, policy) if self.owner.external_context else None
        if self.route_index >= len(policy.routes) or params["route"] != policy.routes[self.route_index]:
            raise Conflict("provider attempt is not the next authorized route")
        route = policy.routes[self.route_index]
        self.route_index += 1
        if route not in self.owner.proposers:
            raise ValueError(f"unconfigured model route: {route}")
        identity, old = latest_model_attempt(state, f"{self.task.id}/step/{self.step}/model/{route}")
        parents: tuple[str, ...] = (self.prior,) if self.prior else ()
        proposal: Outcome | None = None
        replayed = False
        if old:
            if old["outcome"]["status"] == "completed":
                proposal, replayed = Outcome.model_validate_json(canonical(old["outcome"])), True
            elif self.retry is None or self.retry.failed_id != identity:
                return self._provider_failed(identity, policy)
            else:
                parents = (*parents, self.retry.failed_id)
                identity = self.retry.next_id
        if proposal is None:
            if self.context is None:
                self.context = self.owner._context(self.task, state, self.agent)
            try:
                proposal = propose(self.gateway, self.owner.proposers[route], identity,
                    self.task.id, route, self.context, self.specs, parents,
                    expected_budget=expected_budget, harness=self.harness, adoption=self.context_admission,
                    native_checkpoint_required=self.replay is not None)
            except BudgetExceeded:
                self.result = {"status": "budget_exhausted", "step": self.step}
                self.prepared = False
                return {"stop": True, "result": self.result}
        if proposal.status == "unknown":
            raise InDoubt(identity)
        if proposal.status != "completed":
            return self._provider_failed(identity, policy)
        self.proposal_id = identity
        self.owner._message({"role": "assistant", "proposal_id": identity, **proposal.data},
                            self.step, replayed=replayed)
        if self.owner._cancelled():
            self.result = {"status": "cancelled", "step": self.step}
            self.prepared = False
            return {"stop": True, "result": self.result}
        self.plan = None
        self.harness_tools = recorded_harness_tools(self.store, identity)
        if proposal.data.get("schema_version") == TURN_SCHEMA:
            turn = AssistantTurn.model_validate_json(canonical(proposal.data))
            # Pi turns unknown tools and invalid arguments into finalized tool
            # errors. Keep the turn open for that receipt and its checkpoint.
            if not self.harness_tools and len(turn.tool_calls) > 1 and any(c.name == "finish" for c in turn.tool_calls):
                self.result = {"status": "invalid_turn", "step": self.step,
                               "reason": "finish must be the only tool call in its turn"}
                self.prepared = False
                return {"stop": True, "result": self.result}
            self.plan = self.owner._batch_plan(turn, self.task, identity, state)
            self.calls = turn_actions(turn, self.task.id, identity, parallel=self.plan is not None)
            self.text = turn.text
        else:
            self.calls = (Action(id=f"{self.task.id}/step/{self.step}/call", task=self.task.id,
                tool=proposal.data["tool"], arguments=proposal.data["arguments"], parents=(identity,)),)
            self.text = ""
        if not self.harness_tools and self.owner.role == "scout" and any(
                a.tool not in self.allowed and not (
                    a.tool in self.harness_tools and native_name(a.tool, self.native_leaves))
                for a in self.calls):
            raise Conflict("model requested a tool outside this worker role")
        self.outcomes = []
        self.prepared, self.active = False, True
        return {"proposal_id": identity, "text": self.text, "parallel": self.plan is not None,
                "calls": [self.wire(a) for a in self.calls]}

    def _provider_failed(self, identity: str, policy: Policy) -> Json:
        self.failed.append(identity)
        if self.route_index == len(policy.routes):
            self.result = {"status": "providers_failed", "step": self.step,
                           "failed_model_actions": self.failed}
            self.prepared = False
            return {"stop": True, "result": self.result}
        failed: Json = {"failed": True, "receipt": identity}
        if self.owner.external_context:
            assert self.context_source is not None
            state = self.store.snapshot()
            refreshed = {**self.context_source,
                         "budget": {key: state[key] for key in ("limit", "spent", "reserved")}}
            if isinstance(self.store, ManagedStore):
                refreshed["runtime"] = self.store.runtime_state()["stamp"]
            self.context_source = refreshed
            self.context = None
            failed["context_source"] = validate_context_source(refreshed)
        return failed

    @staticmethod
    def wire(action: Action) -> Json:
        return {"id": "kc_" + digest(action.id), "name": action.tool, "arguments": action.arguments}

    def _pending(self, params: Json) -> Action:
        index = len(self.outcomes)
        if not self.active or index >= len(self.calls):
            raise Conflict("no pending tool call")
        action = self.calls[index]
        if canonical(params) != canonical(self.wire(action)):
            raise Conflict("worker tool differs from the durable model response")
        return action

    def tool(self, params: Json) -> Json:
        action = self._pending(params)
        if native_name(action.tool, self.native_leaves):
            raise Conflict("native execution requires its sealed invocation protocol")
        if self.remote_thread is not None or action.tool == "delegate":
            raise Conflict("delegation requires its protected plugin protocol")
        batch = execute_calls((action,), lambda a: self.owner._dispatch(a, self.step),
                              self.owner._observe, self.owner._cancelled)
        if batch.status != "completed":
            self.result = {"status": batch.status, "step": self.step}
            self.active = False
            return {"stop": True, "result": self.result}
        self.outcomes.append(batch.outcomes[0])
        return {"action_id": action.id, "outcome": batch.outcomes[0].model_dump(mode="json")}

    def tool_error(self, params: Json) -> Json:
        """Trusted Pi lifecycle evidence for a call whose executor never started."""
        if (set(params) != {"call", "reason"} or not isinstance(params["reason"], str)
                or len(params["reason"]) > 8192 or not self.owner.external_context):
            raise Conflict("invalid unexecuted tool error")
        action = self._pending(params["call"])
        if action.id in self.native_invocations or action.id in self.store.snapshot()["actions"]:
            raise Conflict("started effects cannot be relabelled as unexecuted")
        outcome = self.gateway.internal(action, 0, lambda: Outcome(status="failed", data={
            "error": params["reason"], "unexecuted": True, "promoted": False}))
        self.owner._observe(action, outcome)
        self.outcomes.append(outcome)
        return {"action_id": action.id, "outcome": outcome.model_dump(mode="json")}

    def batch(self, params: Json) -> Json:
        if (not self.active or self.plan is None or self.outcomes
                or params != {"proposal_id": self.proposal_id}):
            raise Conflict("no sealed parallel batch")
        result = self.owner._parallel_dispatch(self.calls, self.step, self.plan)
        if result.status != "completed":
            self.result = {"status": result.status, "step": self.step}
            self.active = False
            return {"stop": True, "result": self.result}
        self.outcomes = list(result.outcomes)
        return {"results": [{"action_id": a.id, "outcome": o.model_dump(mode="json")}
                            for a, o in zip(self.calls, self.outcomes, strict=True)]}

    def end_turn(self, params: Json) -> Json:
        checked = validate_replay(params, "EndTurnRequest")
        if (not self.active or checked["proposal_id"] != self.proposal_id
                or len(self.calls) != len(self.outcomes) or self.remote_thread is not None):
            raise Conflict("cannot checkpoint an unfinished turn")
        result: Json | None = None
        if not self.calls:
            result = {"status": "finished", "result": {"summary": self.text,
                      "promoted": False, "model_receipt": self.proposal_id}}
        elif self.calls[-1].tool == "finish" and self.outcomes[-1].status == "completed":
            result = {"status": "finished", "result": self.outcomes[-1].data}
        elif all(o.data.get("terminate") is True for o in self.outcomes):
            result = {"status": "finished", "result": {"reason": "tools_terminated", "promoted": False}}
        if self.owner.rsi is not None and result is not None:
            self.owner.rsi.check_finished()
        checkpoint = None
        if self.replay is not None:
            checkpoint = self.replay.checkpoint(checked.get("native_checkpoint"))
        elif "native_checkpoint" in checked:
            raise Conflict("native checkpoint has no bound file owner")
        prior = self.calls[-1].id if self.calls else self.proposal_id
        self.owner._control.checkpoint(self.task.id, self.step, self.proposal_id, prior, result,
                                     parents=tuple(a.id for a in self.calls) if self.plan else None,
                                     native_checkpoint=checkpoint)
        if self.plan is not None:
            prior = f"{self.task.id}/step/{self.step}/end"
        self.owner._emit("turn_end", {"proposal_id": self.proposal_id,
            "outcomes": [o.model_dump(mode="json") for o in self.outcomes]}, self.step)
        self.last_result, self.prior, self.active = result, prior, False
        if self.owner.should_stop_after_turn is not None and self.owner.should_stop_after_turn(self.owner.state):
            self.result = {"status": "paused", "step": self.step}
        self.step += 1
        return {"stop": self.result is not None, "result": self.result}

    def native_begin(self, params: Json) -> Json:
        checked = validate_native_begin(params)
        action = self._pending(dict(checked))
        if not self.owner.external_context or not native_name(action.tool, self.native_leaves):
            raise Conflict("tool is not an enabled native invocation")
        validate_native_arguments(action.tool, action.arguments, self.harness_tools)
        prior = self.native_invocations.get(action.id)
        if prior is not None:
            if prior.closed:
                raise Conflict("native invocation permit is already closed")
            return {"permit": action.id}
        if self.remote_thread is not None or any(
                not invocation.closed for invocation in self.native_invocations.values()):
            raise Conflict("another foreground invocation already owns this session")
        invocation = NativeEffects(
            self.gateway, action, self.native_leaves, self.harness_tools,
            lambda nested: self.owner._dispatch(nested, self.step), self.owner._observe,
            self.owner._cancelled, timeout=self.owner.worker_timeout)
        self.native_invocations[action.id] = invocation
        result = invocation.begin()
        if result.get("cached"):
            self._accept_native(action, Outcome.model_validate_json(canonical(result["outcome"])))
        return result

    def native_call(self, params: Json) -> Json:
        request = validate_native_call(params)
        invocation = self.native_invocations.get(request["permit"])
        if not self.active or invocation is None:
            raise Conflict("native call has no active invocation permit")
        return invocation.call(request)

    def native_settle(self, params: Json) -> Json:
        request = validate_native_settle(params)
        invocation = self.native_invocations.get(request["permit"])
        if invocation is None:
            raise Conflict("native settlement has no matching invocation permit")
        repeated = invocation.settlement is not None
        result = invocation.settle(request)
        if not repeated:
            self._accept_native(invocation.action,
                                Outcome.model_validate_json(canonical(result["outcome"])))
        return result

    def _accept_native(self, action: Action, outcome: Outcome) -> None:
        if len(self.outcomes) >= len(self.calls) or self.calls[len(self.outcomes)] != action:
            raise Conflict("native result is not the pending sealed call")
        if outcome.status == "unknown":
            raise InDoubt(action.id)
        self.owner._observe(action, outcome)
        self.outcomes.append(outcome)

    def plugin_begin(self, params: Json) -> Json:
        action = self._pending(params)
        if action.tool != "delegate" or self.remote_thread is not None:
            raise Conflict("invalid foreground delegation")
        remote = self.gateway.tools.get("delegate")
        if remote is None or not all(hasattr(remote, n) for n in ("arm", "resolve", "clear", "started")):
            raise Conflict("delegation effect not installed")
        self.remote = remote
        self.remote.arm(action.id)
        self.remote_action, self.remote_result = action, Future()
        def dispatch() -> None:
            assert self.remote_result is not None
            try:
                self.remote_result.set_result(self.owner._dispatch(action, self.step))
            except BaseException as error:
                self.remote_result.set_exception(error)
        self.remote_thread = Thread(target=dispatch, name="kernelcat-delegation-effect", daemon=True)
        self.remote_thread.start()
        while not self.remote.started.wait(0.02):
            if self.remote_result.done():
                outcome = self._collect_plugin()
                return {"cached": True, "outcome": outcome.model_dump(mode="json")}
            if self.owner._cancelled():
                self.close()
                raise CallCancelled()
        self.child = DelegatedEffects(self.owner, action, self.proposal_id)
        return {"permit": action.id, "agent": action.arguments["agent"],
                "task": action.arguments["task"], "timeout_ms": 60000,
                "routes": list(self.store.policy().routes),
                "mode": self.child.mode, "max_steps": self.child.max_steps}


    def _child(self, params: Json) -> DelegatedEffects:
        if (self.remote_thread is None or self.remote_action is None or self.child is None
                or params.get("permit") != self.remote_action.id
                or self.remote_result is None or self.remote_result.done()):
            raise Conflict("child request has no live delegation permit")
        return self.child

    def child_model(self, params: Json) -> Json:
        child = self._child(params)
        if set(params) != {"permit", "route"}:
            raise Conflict("invalid child model request")
        return child.model(params["route"])

    def child_tool(self, params: Json) -> Json:
        child = self._child(params)
        if set(params) != {"permit", "call"} or not isinstance(params["call"], dict):
            raise Conflict("invalid child tool request")
        return child.tool(params["call"])

    def plugin_settle(self, params: Json) -> Json:
        if (self.remote_thread is None or self.remote_action is None
                or set(params) != {"permit", "status", "text"}
                or params["permit"] != self.remote_action.id or params["status"] not in {"completed", "failed"}
                or not isinstance(params["text"], str) or len(params["text"].encode()) > 65536):
            raise Conflict("invalid child settlement")
        child = self._child(params)
        if params["status"] == "completed":
            if child.summary is None or params["text"] != child.summary or child.failure is not None:
                raise Conflict("child output differs from its authoritative model receipt")
            outcome = Outcome(data={"summary": child.summary, "model_receipt": child.receipt,
                "plugin": "pi-subagents@0.87.0", "advisory": True, "promoted": False,
                "mode": child.mode, "steps": child.step + 1, "tool_calls": child.tool_count})
        else:
            outcome = Outcome(status="failed", data={"error": "foreground subagent failed",
                "reason": child.failure or "plugin_failed", "model_receipt": child.receipt,
                "advisory": True})
        self.remote.resolve(outcome)
        settled = self._collect_plugin()
        return {"outcome": settled.model_dump(mode="json")}

    def _collect_plugin(self) -> Outcome:
        assert self.remote_thread is not None and self.remote_result is not None
        self.remote_thread.join()
        try:
            result = self.remote_result.result()
            if result.status == "unknown":
                raise InDoubt("delegation has unknown effects")
            self.owner._observe(self.remote_action, result)
            self.outcomes.append(result)
            return result
        finally:
            if not self.remote.future.done():
                self.remote.resolve(Outcome(status="unknown"))
            self.remote.clear()
            self.remote_thread, self.remote_result, self.remote_action = None, None, None

    def close(self) -> None:
        for invocation in self.native_invocations.values():
            invocation.close()
        if self.remote_thread is not None:
            if not self.remote.future.done():
                self.remote.resolve(Outcome(status="unknown", data={"reason": "Pi worker interrupted"}))
            self.remote_thread.join()
            self.remote.clear()
            self.remote_thread, self.remote_result, self.remote_action = None, None, None

    def handle(self, method: str, params: Json) -> Json:
        if method in {"prepare_turn", "complete"} and params:
            raise Conflict("unexpected control parameters")
        if method == "hello":
            return self.hello(params)
        if not self.initialized:
            raise Conflict("worker handshake required")
        if method.startswith("rsi_"):
            return self._rsi(method, params)
        if method == "prepare_turn":
            return self.prepare_turn()
        if method == "model":
            return self.model(params)
        if method == "tool":
            return self.tool(params)
        if method == "tool_error":
            return self.tool_error(params)
        if method == "batch":
            return self.batch(params)
        if method == "native_begin":
            return self.native_begin(params)
        if method == "native_call":
            return self.native_call(params)
        if method == "native_settle":
            return self.native_settle(params)
        if method == "end_turn":
            return self.end_turn(params)
        if method == "plugin_begin":
            return self.plugin_begin(params)
        if method == "child_model":
            return self.child_model(params)
        if method == "child_tool":
            return self.child_tool(params)
        if method == "plugin_settle":
            return self.plugin_settle(params)
        if method == "complete" and not self.active and self.result is not None:
            return self.result
        raise Conflict("invalid or premature worker operation")

    def _rsi(self, method: str, params: Json) -> Json:
        rsi: RsiSession | None = self.owner.rsi
        if rsi is None:
            raise Conflict("RSI is not configured")
        self._unresolved()
        if self.prepared:
            raise Conflict("RSI effects cannot change a prepared model context")
        if method == "rsi_status":
            if params:
                raise Conflict("rsi_status takes no parameters")
            if self.owner._cancelled():
                self.result = {"status": "cancelled", "step": self.step}
            if self.step >= self.store.policy().max_steps:
                self.result = self.last_result or {"status": "step_limit", "steps": self.step}
            if self.result is None and self.last_result is not None and not self.active:
                inputs = self.owner._control.take(self.task.id, self.step, terminal=True)
                if not inputs:
                    self.result = self.last_result
            return rsi.status(self.step, self.result is not None)
        if self.active and (len(self.calls) != len(self.outcomes) or self.remote_thread is not None):
            raise Conflict("RSI effects require settled model tools")
        if method in {"rsi_adapter", "rsi_apply"} and not self.active:
            raise Conflict("RSI evaluation requires an active, settled model turn")
        if method in {"rsi_context", "rsi_effect"} and self.active:
            raise Conflict("RSI context reads require an idle model boundary")
        return rsi.handle(method, params, self.step)
