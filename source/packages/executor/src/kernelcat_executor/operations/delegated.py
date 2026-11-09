"""Finite effects for plugin-owned child sessions; no child Agent scheduling loop."""
from __future__ import annotations

import time
from typing import Any

from kernelcat_executor.contracts.messages import AssistantTurn, turn_actions
from kernelcat_executor.contracts.models import (
    same_json_value,
    Action,
    BudgetExceeded,
    Conflict,
    InDoubt,
    Json,
    Outcome,
    Task,
    canonical,
    digest,
)
from kernelcat_executor.operations.context import ContextOverflow, project
from kernelcat_executor.operations.control import SessionControl
from kernelcat_executor.operations.requests import propose

READ_TOOLS = frozenset({"read_file", "load_skill", "read_receipt", "read_learned_skill",
                        "knowledge_search", "knowledge_show", "knowledge_source", "knowledge_read"})


class DelegatedEffects:
    """One plugin permit, restricted tool view, shared credits, host-authored history.

    Pi requests the next model attempt. This class can only validate and perform
    that one attempt or one sealed read. A finished child cannot be restarted by
    sending more model requests. Unknown effects always block parent settlement.
    """
    def __init__(self, owner: Any, action: Action, proposal_id: str) -> None:
        self.owner, self.action = owner, action
        self.mode = action.arguments.get("mode", "analysis")
        self.max_steps = 1 if self.mode == "analysis" else action.arguments.get("max_steps", 6)
        self.gateway = owner.gateway.restrict(READ_TOOLS)
        self.store = self.gateway.store
        self.task = Task(id=action.id + "/leaf", goal=action.arguments["task"],
                         scope=self.gateway.scope, contract=owner._task.contract)
        self.store.register_task(self.task.model_dump(mode="json"))
        self.specs = () if self.mode == "analysis" else self.gateway.specifications()
        self.control = SessionControl(self.gateway, owner.options)
        self.prior = proposal_id
        self.step = self.route_index = self.tool_count = 0
        self.calls: tuple[Action, ...] = ()
        self.outcomes: list[Outcome] = []
        self.receipt: str | None = None
        self.summary: str | None = None
        self.failure: str | None = None
        self.deadline = time.monotonic() + 60

    def _stop(self, reason: str) -> Json:
        self.failure = reason
        return {"failed": True, "stop": True, "reason": reason, "receipt": self.receipt}

    def _check(self) -> Json | None:
        if self.failure is not None:
            return self._stop(self.failure)
        if self.owner._cancelled():
            return self._stop("cancelled")
        if time.monotonic() >= self.deadline:
            return self._stop("timeout")
        return None

    def model(self, route: str) -> Json:
        if self.summary is not None:
            raise Conflict("child already has a final model receipt")
        stopped = self._check()
        if stopped:
            return stopped
        if self.calls:
            if len(self.outcomes) != len(self.calls):
                raise Conflict("child tools must settle before another model request")
            assert self.receipt is not None
            self.prior = self.calls[-1].id
            self.control.checkpoint(self.task.id, self.step, self.receipt, self.prior, None)
            self.calls, self.outcomes = (), []
            self.step += 1
            self.route_index = 0
        if self.step >= self.max_steps:
            return self._stop("step_limit")
        policy = self.store.policy()
        if self.route_index >= len(policy.routes) or route != policy.routes[self.route_index]:
            raise Conflict("child route is not the next authorized provider")
        self.route_index += 1
        model = self.owner.child_proposers.get(route)
        if model is None:
            return self._stop("provider_unconfigured")
        identity = f"{self.task.id}/step/{self.step}/model/{route}"
        context = project(self.task, policy, self.store.snapshot(), self.owner.skills)
        context["instruction"] = (
            "Return an advisory answer. Never invent measurements. "
            + ("Use only supplied material; no tools." if self.mode == "analysis" else
               "Inspect the candidate, skill and receipt evidence using only the offered read tools. "
               "No writing, shell, device execution, publication or further delegation is permitted.")
        )
        if len(canonical(context).encode()) > policy.context_bytes:
            raise ContextOverflow("child instruction and context exceed the model context bound")
        try:
            outcome = propose(self.gateway, model, identity, self.task.id, route,
                              context, self.specs, (self.prior,))
        except BudgetExceeded:
            return self._stop("budget_exhausted")
        self.receipt = identity
        if outcome.status == "unknown":
            raise InDoubt(identity)
        if outcome.status != "completed":
            if self.route_index == len(policy.routes):
                return self._stop("providers_failed")
            return {"failed": True, "receipt": identity}
        turn = AssistantTurn.model_validate_json(canonical(outcome.data))
        if len(turn.text.encode()) > 65536:
            return self._stop("output_limit")
        if self.mode == "analysis" and turn.tool_calls:
            return self._stop("unexpected_tool")
        self.calls = turn_actions(turn, self.task.id, identity)
        if not self.calls:
            self.summary = turn.text
            self.control.checkpoint(self.task.id, self.step, identity, identity,
                                    {"status": "finished", "advisory": True})
        return {"text": turn.text, "calls": [self.wire(a) for a in self.calls],
                "receipt": identity, "usage": turn.usage}

    @staticmethod
    def wire(action: Action) -> Json:
        return {"id": "kc_" + digest(action.id), "name": action.tool, "arguments": action.arguments}

    def tool(self, call: Json) -> Json:
        stopped = self._check()
        if stopped:
            return stopped
        index = len(self.outcomes)
        if index >= len(self.calls) or not same_json_value(call, self.wire(self.calls[index])):
            raise Conflict("child tool differs from its recorded model reply")
        if self.tool_count >= 16:
            return self._stop("tool_limit")
        action = self.calls[index]
        result = self.gateway.execute(action)
        if result.status == "unknown":
            raise InDoubt(action.id)
        self.tool_count += 1
        self.outcomes.append(result)
        if result.data.get("reason") == "budget":
            return self._stop("budget_exhausted")
        return {"action_id": action.id, "outcome": result.model_dump(mode="json")}
