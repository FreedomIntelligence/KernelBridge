"""Receipt/effect RPC for Pi. No provider-selection or model/tool scheduling loop.

Pi requests one provider attempt, one finite tool batch, or one checkpoint.
This authority verifies requests against durable facts and never advances an
agent on its own. Conversational scheduling is exclusively in the Pi SDK.
"""
from __future__ import annotations

import time

from concurrent.futures import Future
from threading import RLock, Thread
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
    ToolFailure,
    canonical,
    same_json_value,
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
from kernelcat_executor.operations.recovery import ModelRetry, latest_model_attempt, model_chain_length
from kernelcat_executor.operations.recovery import _successor as retry_successor
from kernelcat_executor.operations.retry_policy import failure_summary, retry_delay
from kernelcat_executor.operations.requests import propose, recorded_harness_tools
from kernelcat_executor.state.goal_progress import model_call_progress, with_model_call_progress
from kernelcat_executor.operations.turns import CallCancelled, execute_calls

SCOUT_TOOLS = frozenset({"read_file", "load_skill", "read_receipt", "read_learned_skill", "finish"})


class ChildRun:
    """One delegate action: its Gateway dispatch thread and the plugin-side effects."""
    def __init__(self, action: Action, index: int) -> None:
        self.action, self.index = action, index
        self.thread: Thread | None = None
        self.result: Future[Outcome] = Future()
        self.effects: DelegatedEffects | None = None
        self.closed = False


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
        self.failures: list[Json] = []
        self.proposal_id = ""
        self.text, self.thinking = "", ""
        self.calls: tuple[Action, ...] = ()
        self.outcomes: list[Outcome] = []
        self.plan: Any = None
        self.specs = tuple(s for s in self.gateway.specifications()
                           if owner.role != "scout" or s.name in SCOUT_TOOLS)
        self.allowed = {s.name for s in self.specs}
        self.remote: Any = None
        # Live delegations keyed by permit (the delegate action id). Several
        # delegate calls from one model reply may run concurrently; results are
        # recorded in reply order once every earlier call has settled.
        self.children: dict[str, ChildRun] = {}
        self.child_results: dict[int, tuple[Action, Outcome]] = {}
        self.child_lock = RLock()
        # Background delegations outlive the reply that started them. Their
        # delegate call settles at once; the child's own result is recorded
        # later and announced to the conversation by the host.
        self.background: dict[str, ChildRun] = {}
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
        if self.result is not None and self.result.get("status") == "finished" and self.owner._control.pending(self.task.id):
            self.result = None
        if self.result is not None:
            return {"stop": True, "result": self.result}
        state = self.store.snapshot()
        policy = Policy.model_validate_json(canonical(state["policy"]))
        application = self.store.application_state() if hasattr(self.store, "application_state") else None
        trial_start = (application["trials"][-1].get("start_step", 0)
                       if application and application["trials"] else 0)
        if policy.max_steps is not None and self.step - trial_start >= policy.max_steps:
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
        self.context, self.route_index, self.failed, self.failures = None, 0, [], []
        self.harness = None
        self.harness_tools = {}
        self.prepared = True
        prepared: Json = {"routes": list(policy.routes), "step": self.step}
        if self.owner.external_context:
            context_task = self.task.model_copy(update={"goal": application["task"]["goal"],
                "contract": application["task"].get("contract", {})}) if application else self.task
            project_session = application and self.owner.workspace_policy.get("mode") == "project"
            if project_session and (not application["goal"] or application["goal"]["status"] == "cleared"):
                # Pi owns conversation boundaries. A fresh /new must not receive
                # the initial task or old user requests via the authority prefix.
                context_task = context_task.model_copy(update={"goal": "Follow the user's request in the current conversation."})
            self.context_source = source(context_task, policy, self.store.snapshot(),
                self.owner.skills, agent=self.agent, goal_progress=model_call_progress(self.store))
            if project_session:
                from kernelcat_executor.operations.control import input_identity
                session_id = application.get("checkpoint", {}).get("pi_session_id")
                current_ids = {input_identity(self.task.id, item["input_id"]) for item in application["inputs"].values()
                               if item.get("pi_session_id") == session_id}
                self.context_source["inputs"] = [{**batch, "messages": [message for message in batch["messages"]
                    if message["id"] in current_ids]} for batch in self.context_source.get("inputs", [])]
                self.context_source["inputs"] = [batch for batch in self.context_source["inputs"] if batch["messages"]]
                if not self.context_source["inputs"]:
                    self.context_source.pop("inputs")
            if isinstance(self.store, ManagedStore):
                self.context_source["runtime"] = self.store.runtime_state()["stamp"]
            if self.owner.rsi is not None:
                self.context_source["rsi"] = self.owner.rsi.context(self.step)
            prepared["context_source"] = validate_context_source(self.context_source)
        return prepared

    def _submitted_context(self, params: Json, policy: Policy) -> ContextBudget | None:
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
                dict(self.harness) if self.harness is not None else submitted,
                concurrent_budget=getattr(self.owner, "concurrent_delegation", False))
        elif "commit" in checked:
            raise Conflict("legacy session cannot adopt a managed ContextCommit")
        # The selected prompt retains its genuine observed budget. Background
        # children may spend/reserve between that observation and this atomic
        # admission, so the live ledger (not that old observation) authorizes
        # spending. Policy, source and selected input checks above stay strict.
        return None if getattr(self.owner, "concurrent_delegation", False) else checked["context"]["budget"]

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
                return self._provider_failed(identity, policy, route, old["outcome"].get("data", {}).get("failure_details"))
            else:
                parents = (*parents, self.retry.failed_id)
                identity = self.retry.next_id
        if proposal is None:
            if self.context is None or not self.owner.external_context:
                # Legacy provider fallbacks are separate admitted model calls.
                # Re-project before preparing their bytes, just as an external
                # client receives refreshed source after each failed attempt.
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
            return self._provider_failed(identity, policy, route, proposal.data.get("failure_details"))
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
            self.plan = (self.owner._batch_plan(turn, self.task, identity, state)
                         or self.owner._delegation_plan(turn, self.task, identity, state))
            self.calls = turn_actions(turn, self.task.id, identity, parallel=self.plan is not None)
            self.text, self.thinking = turn.text, turn.reasoning
        else:
            self.calls = (Action(id=f"{self.task.id}/step/{self.step}/call", task=self.task.id,
                tool=proposal.data["tool"], arguments=proposal.data["arguments"], parents=(identity,)),)
            self.text, self.thinking = "", ""
        if not self.harness_tools and self.owner.role == "scout" and any(
                a.tool not in self.allowed and not (
                    a.tool in self.harness_tools and native_name(a.tool, self.native_leaves))
                for a in self.calls):
            raise Conflict("model requested a tool outside this worker role")
        self.outcomes = []
        self.prepared, self.active = False, True
        return {"proposal_id": identity, "text": self.text, "parallel": self._gateway_batch(),
                "calls": [self.wire(a) for a in self.calls], "usage": proposal.data.get("usage", {}),
                **({"thinking": self.thinking} if self.thinking else {})}

    def _automatic_retry(self, identity: str, route: str, details: Json) -> Json | None:
        """Permit one more attempt on the same route after a transient failure.

        The next attempt is the deterministic successor of this receipt, so the
        Journal holds every attempt and resume follows the same bounded chain."""
        proposer = self.owner.proposers.get(route)
        state = self.store.snapshot()
        retries = model_chain_length(state, f"{self.task.id}/step/{self.step}/model/{route}") - 1
        delay = retry_delay(details, retries, getattr(proposer, "max_retries", 0))
        if delay is None:
            return None
        self.route_index -= 1
        self.retry = ModelRetry(identity, retry_successor(identity))
        return {"route": route, "attempt": retries + 1, "max_retries": proposer.max_retries,
                "delay_ms": int(delay * 1000), **failure_summary(details)}

    def _provider_failed(self, identity: str, policy: Policy, route: str | None = None,
                         details: Json | None = None) -> Json:
        self.failed.append(identity)
        details = details if isinstance(details, dict) else {}
        self.failures.append({"route": route, **failure_summary(details)})
        retry = self._automatic_retry(identity, route, details) if route is not None else None
        if retry is None and self.route_index == len(policy.routes):
            self.result = {"status": "providers_failed", "step": self.step,
                           "failed_model_actions": self.failed, "failures": self.failures}
            self.prepared = False
            return {"stop": True, "result": self.result}
        failed: Json = {"failed": True, "receipt": identity, **({"retry": retry} if retry else {})}
        if self.owner.external_context:
            assert self.context_source is not None
            state = self.store.snapshot()
            refreshed = {**self.context_source,
                         "budget": {key: state[key] for key in ("limit", "spent", "reserved")}}
            if isinstance(self.store, ManagedStore):
                refreshed["runtime"] = self.store.runtime_state()["stamp"]
            refreshed = with_model_call_progress(refreshed, self.store)
            self.context_source = refreshed
            self.context = None
            failed["context_source"] = validate_context_source(refreshed)
        return failed

    @staticmethod
    def wire(action: Action) -> Json:
        return {"id": "kc_" + digest(action.id), "name": action.tool, "arguments": action.arguments}

    def _gateway_batch(self) -> bool:
        return self.plan is not None and self.plan.execution_mode == "parallel"

    def _pending(self, params: Json) -> Action:
        index = len(self.outcomes)
        if not self.active or index >= len(self.calls):
            raise Conflict("no pending tool call")
        action = self.calls[index]
        if not same_json_value(params, self.wire(action)):
            raise Conflict("worker tool differs from the durable model response")
        return action

    def tool(self, params: Json) -> Json:
        action = self._pending(params)
        if native_name(action.tool, self.native_leaves):
            raise Conflict("native execution requires its sealed invocation protocol")
        if self.children or action.tool == "delegate":
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
        if (not self.active or not self._gateway_batch() or self.outcomes or self.children
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
                or len(self.calls) != len(self.outcomes) or self.children or self.child_results):
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

    def interrupt_turn(self) -> Json:
        """Finalize only provably unstarted calls; active effects remain fenced.

        The caller must persist the matching native error/recovery entries and
        acknowledge end_turn with that checkpoint before another model request.
        """
        self._unresolved()
        if self.children:
            raise InDoubt("delegated work must settle before interruption")
        if not self.active:
            self.prepared = False
            return {"needs_checkpoint": False, "calls": []}
        skipped = []
        while len(self.outcomes) < len(self.calls):
            action = self.calls[len(self.outcomes)]
            call = self.wire(action)
            receipt = self.tool_error({"call": call, "reason": "User interrupted before this tool started"})
            skipped.append({"call": call, "receipt": receipt})
        return {"needs_checkpoint": True, "proposal_id": self.proposal_id, "calls": skipped}

    def end_failed_turn(self, params: Json) -> Json:
        """Close a known failed model turn; a later input gets a new action ID."""
        self._unresolved()
        if (self.active or self.prepared or not self.failed or not self.result
                or self.result.get("status") != "providers_failed" or set(params) != {"native_checkpoint"} or self.replay is None):
            raise Conflict("only a settled provider failure may close this turn")
        checkpoint = self.replay.checkpoint(params["native_checkpoint"])
        identity = self.failed[-1]
        self.owner._control.checkpoint(self.task.id, self.step, identity, identity, None, native_checkpoint=checkpoint)
        self.prior, self.result, self.last_result = identity, None, None
        self.step += 1
        return {"status": "failed", "model_receipt": identity}

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
        if self.children or any(
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

    def _pending_delegate(self, params: Json) -> tuple[Action, int]:
        """A not-yet-started delegate within the contiguous delegate run at the cursor."""
        if not self.active:
            raise Conflict("no pending tool call")
        for index in range(len(self.outcomes), len(self.calls)):
            action = self.calls[index]
            if action.tool != "delegate":
                break
            if action.id in self.children or index in self.child_results:
                continue
            if same_json_value(params, self.wire(action)):
                return action, index
        raise Conflict("invalid foreground delegation")

    def plugin_begin(self, params: Json) -> Json:
        with self.child_lock:
            action, index = self._pending_delegate(params)
            remote = self.gateway.tools.get("delegate")
            if remote is None or not all(hasattr(remote, n) for n in ("arm", "resolve", "clear", "done")):
                raise Conflict("delegation effect not installed")
            self.remote = remote
            settings = remote.settings
            prior_children = sum(a.tool == "delegate" for a in self.calls[:index])
            total = sum(item["action"]["tool"] == "delegate" for item in self.store.snapshot()["actions"].values())
            live = sum(not run.closed for run in (*self.children.values(), *self.background.values()))
            denial = (("count_limit", "Configured subagent count reached")
                      if settings.get("max_per_turn") is not None and prior_children >= settings["max_per_turn"]
                      or settings.get("max_total") is not None and total >= settings["max_total"] else
                      ("concurrency_limit", "Configured concurrent subagent limit reached; retry after a child finishes")
                      if settings.get("max_concurrent") is not None and live >= settings["max_concurrent"] else None)
            if remote.agents.get(action.arguments["agent"], {}).get("tools") == "test" and action.arguments.get("background"):
                denial = ("background_requires_isolation", "Test agents use the shared project and must run in the foreground. "
                          "Omit background or use a worktree worker for isolated background commands.")
            if any(item["action"]["task"] == self.task.id and item["action"]["tool"] != "delegate"
                   and not item.get("terminal") for item in self.store.snapshot()["actions"].values()):
                denial = ("foreground_busy", "Settle the parent project operation before starting a child.")
            started = remote.arm(action.id, denial)
            run = ChildRun(action, index)

            def dispatch() -> None:
                try:
                    run.result.set_result(self.owner._dispatch(action, self.step))
                except BaseException as error:
                    run.result.set_exception(error)
            run.thread = Thread(target=dispatch, name="kernelcat-delegation-effect", daemon=True)
            self.children[action.id] = run
            run.thread.start()
        while not started.wait(0.02):
            if run.result.done():
                outcome = self._collect_plugin(action.id)
                return {"cached": True, "outcome": outcome.model_dump(mode="json")}
            if self.owner._cancelled():
                self._abandon(action.id)
                raise CallCancelled()
        try:
            run.effects = DelegatedEffects(self.owner, action, self.proposal_id)
        except (ToolFailure, FileNotFoundError) as error:
            # Setup has not dispatched a child effect. Its rejection is known,
            # unlike a provider/tool whose original receipt is still pending.
            self.remote.resolve(action.id, Outcome(status="failed", data={
                "reason": "child_setup_failed", "error": str(error)[:400], "unexecuted": True}))
            outcome = self._collect_plugin(action.id)
            return {"cached": True, "outcome": outcome.model_dump(mode="json")}
        # Background needs a host that announces results and runs children
        # concurrently; elsewhere the same call simply runs in the foreground.
        if (action.arguments.get("background") is True and settings.get("background", True)
                and getattr(self.owner, "concurrent_delegation", False)):
            started_outcome = Outcome(data={"background": True, "agent_id": action.id, "agent": action.arguments["agent"],
                "status": "running", "advisory": True, "promoted": False,
                **({"worktree": run.effects.worktree.descriptor()} if run.effects.worktree else {})})
            self.remote.resolve(action.id, started_outcome)
            collected = self._collect_plugin(action.id, background=True)
            return {"permit": action.id, "agent": action.arguments["agent"], "task": action.arguments["task"],
                    "timeout_ms": settings["timeout"] * 1000 if settings.get("timeout") else None,
                    "backstop_ms": self._backstop_ms(settings, run.effects.routes),
                    "tool_limit": settings.get("tool_limit"), "routes": list(run.effects.routes),
                    "mode": run.effects.mode, "max_steps": run.effects.max_steps,
                    "background": True, "outcome": collected.model_dump(mode="json")}
        return {"permit": action.id, "agent": action.arguments["agent"],
                "task": action.arguments["task"],
                "timeout_ms": settings["timeout"] * 1000 if settings.get("timeout") else None,
                "backstop_ms": self._backstop_ms(settings, run.effects.routes),
                "tool_limit": settings.get("tool_limit"),
                "routes": list(run.effects.routes),
                "mode": run.effects.mode, "max_steps": run.effects.max_steps}

    def _backstop_ms(self, settings: Json, routes) -> int | None:
        """The plugin's transport backstop: after the host's own deadline, the final
        summary grace and one provider call, so a child that is merely finishing up
        never turns into a parent runtime failure."""
        if not settings.get("timeout"):
            return None
        timeout = float(settings["timeout"])
        grace = min(60.0, max(10.0, 0.1 * timeout))
        provider = max((float(getattr(self.owner.child_proposers.get(route), "timeout", 120.0)) for route in routes), default=120.0)
        return int((timeout + grace + provider) * 1000) + 5000

    def _child(self, params: Json) -> DelegatedEffects:
        with self.child_lock:
            run = self.children.get(params.get("permit"))
            if run is not None and run.effects is not None and not run.result.done():
                return run.effects
            run = self.background.get(params.get("permit"))
            if run is not None and run.effects is not None and not run.closed:
                return run.effects
            raise Conflict("child request has no live delegation permit")

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
        if (set(params) != {"permit", "status", "text"} or params["status"] not in {"completed", "failed", "cancelled"}
                or not isinstance(params["text"], str) or len(params["text"].encode()) > 65536):
            raise Conflict("invalid child settlement")
        child = self._child(params)
        with child.operation_lock:
            outcome = self._child_outcome(child, params)
            if params["permit"] in self.background:
                self._finish_background(params["permit"], outcome)
                return {"outcome": outcome.model_dump(mode="json")}
            self.remote.resolve(params["permit"], outcome)
            settled = self._collect_plugin(params["permit"])
            return {"outcome": settled.model_dump(mode="json")}

    def _child_outcome(self, child: DelegatedEffects, params: Json) -> Outcome:
        # Child model/read RPCs and settlement share the child's ordered lane.
        # A cancellation may discard a reply in Pi, but cannot discard its
        # original receipt or turn an unknown effect into a known failure.
        for item in self.store.snapshot()["actions"].values():
            if item["action"]["task"] == child.task.id and (
                    not item.get("terminal") or item.get("outcome", {}).get("status") == "unknown"):
                raise InDoubt(f"unresolved child action {item['action']['id']}")
        if params["status"] == "cancelled":
            child.failure = "cancelled"
        if params["status"] == "completed" and (
                child.summary is None or params["text"] != child.summary or child.failure is not None):
            raise Conflict("child output differs from its authoritative model receipt")
        changes = {}
        if child.worktree is not None:
            label = child.action.arguments.get("description") or child.action.arguments["task"][:60]
            try:
                changes = {"worktree": child.worktree.finish(f"kernelbridge subagent: {label}")}
            except Exception as error:  # the branch keeps whatever was written
                changes = {"worktree": {"changed": None, "error": str(error)[:400]}}
        if params["status"] == "completed":
            outcome = Outcome(data={"summary": child.summary, "model_receipt": child.receipt,
                "plugin": "pi-subagents@0.87.0", "advisory": True, "promoted": False,
                "agent": child.action.arguments["agent"], "completion_reason": child.completion_reason,
                "partial": child.completion_reason != "answer",
                "mode": child.mode, "steps": child.step + 1, "tool_calls": child.tool_count,
                "elapsed_ms": int((time.monotonic() - child.started_at) * 1000), **changes})
        else:
            # The parent decides what to do next; give it what the child had found.
            outcome = Outcome(status="failed", data={"error": "subagent failed",
                "reason": child.failure or "plugin_failed", "model_receipt": child.receipt,
                "agent": child.action.arguments["agent"], "steps": child.step + 1, "tool_calls": child.tool_count,
                "elapsed_ms": int((time.monotonic() - child.started_at) * 1000),
                **({"partial_text": child.last_text[:4096]} if child.last_text else {}),
                "advisory": True, **changes})
        return outcome

    def _finish_background(self, identity: str, outcome: Outcome) -> None:
        """Record a background child's own result and let the host announce it."""
        with self.child_lock:
            run = self.background.get(identity)
            if run is None or run.closed:
                return
            recorded = self.gateway.internal(Action(id=identity + "/background", task=run.action.task,
                tool="__background_result__", arguments={"agent_id": identity}, parents=(identity,)), 0,
                lambda: outcome)
            # Keep the live permit if persistence failed; recovery uses this
            # exact action id and replays the notification after a crash.
            run.closed = True
            self.background.pop(identity, None)
        hook = getattr(self.owner, "background_settled", None)
        if hook is not None:
            hook(run.action, recorded)

    def _flush_children(self) -> None:
        """Record settled delegations in reply order; later ones wait for earlier ones."""
        while len(self.outcomes) in self.child_results:
            action, outcome = self.child_results.pop(len(self.outcomes))
            self.owner._observe(action, outcome)
            self.outcomes.append(outcome)

    def _collect_plugin(self, identity: str, *, background: bool = False) -> Outcome:
        run = self.children[identity]
        assert run.thread is not None
        run.thread.join()
        try:
            result = run.result.result()
            if result.status == "unknown":
                raise InDoubt("delegation has unknown effects")
            with self.child_lock:
                self.child_results[run.index] = (run.action, result)
                self._flush_children()
                if background:
                    self.background[identity] = run
            return result
        finally:
            if not self.remote.done(identity):
                self.remote.resolve(identity, Outcome(status="unknown"))
            self.remote.clear(identity)
            with self.child_lock:
                self.children.pop(identity, None)

    def _abandon(self, identity: str, reason: str | None = None) -> None:
        run = self.children.get(identity)
        if run is None:
            return
        if not self.remote.done(identity):
            self.remote.resolve(identity, Outcome(status="unknown", data={"reason": reason} if reason else {}))
        if run.thread is not None:
            run.thread.join()
        self.remote.clear(identity)
        with self.child_lock:
            self.children.pop(identity, None)

    def close(self) -> None:
        # Signal first, before waiting on any individual child. Commands kill
        # their process group; in-flight providers retain their real receipts.
        with self.child_lock:
            runs = list(self.children.values()) + list(self.background.values())
        for run in runs:
            if run.effects is not None:
                run.effects.cancel("worker_exited")
        for invocation in self.native_invocations.values():
            invocation.close()
        for identity in list(self.children):
            self._abandon(identity, "Pi worker interrupted")
        for identity, run in list(self.background.items()):
            child = run.effects
            if child is None or not child.operation_lock.acquire(timeout=2):
                continue  # Original in-flight effects remain fenced on recovery.
            try:
                child.failure = "worker_exited"
                outcome = self._child_outcome(child, {"status": "failed", "text": ""})
                self._finish_background(identity, outcome)
            except InDoubt:
                # Never commit/clean a worktree or fabricate an interruption
                # receipt while a model/tool's actual outcome is unknown.
                pass
            finally:
                child.operation_lock.release()

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
        if method == "interrupt_turn" and not params:
            return self.interrupt_turn()
        if method == "end_failed_turn":
            return self.end_failed_turn(params)
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
            if self.store.policy().max_steps is not None and self.step >= self.store.policy().max_steps:
                self.result = self.last_result or {"status": "step_limit", "steps": self.step}
            if self.result is None and self.last_result is not None and not self.active:
                inputs = self.owner._control.take(self.task.id, self.step, terminal=True)
                if not inputs:
                    self.result = self.last_result
            return rsi.status(self.step, self.result is not None)
        if self.active and (len(self.calls) != len(self.outcomes) or self.children):
            raise Conflict("RSI effects require settled model tools")
        if method in {"rsi_adapter", "rsi_apply"} and not self.active:
            raise Conflict("RSI evaluation requires an active, settled model turn")
        if method in {"rsi_context", "rsi_effect"} and self.active:
            raise Conflict("RSI context reads require an idle model boundary")
        return rsi.handle(method, params, self.step)
