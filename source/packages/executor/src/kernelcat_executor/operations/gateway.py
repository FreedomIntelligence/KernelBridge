"""Single authorization pipeline: request -> check -> reserve -> start -> settle."""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping

from jsonschema import Draft202012Validator

from kernelcat_executor.contracts.models import (
    Action,
    BudgetExceeded,
    Conflict,
    InDoubt,
    Outcome,
    Policy,
    ToolFailure,
    ToolSpec,
    canonical,
    digest,
)
from kernelcat_executor.contracts.ports import ResourceStore, Store, Tool
from kernelcat_executor.contracts.runtime import ContextAdmission
from kernelcat_executor.generated.context import ContextBudget
from kernelcat_executor.operations.receipts import ReceiptReader
from kernelcat_executor.operations.registry import (
    ExecutionGuard,
    GuardChain,
    ToolBinding,
    ToolRegistry,
)


class Gateway:
    def __init__(self, store: Store, tools: list[Tool], scope: str,
                 guards: tuple[ExecutionGuard, ...] = ()) -> None:
        self.store, self.scope = store, scope
        if any(t.spec.name == "read_receipt" for t in tools):
            raise ValueError("read_receipt is a reserved tool name")
        self._registry = ToolRegistry([*tools, ReceiptReader(store)])
        self._guards = GuardChain(guards)

    @property
    def tools(self) -> Mapping[str, Tool]:
        """Inspection-only map. Trusted implementations own their internals."""
        return self._registry.tools

    def specifications(self) -> tuple[ToolSpec, ...]:
        return self._registry.specifications()

    def restrict(self, names: frozenset[str]) -> Gateway:
        """A sealed capability view sharing the original ledger and all guards."""
        view = Gateway(self.store, [tool for name, tool in self.tools.items()
                       if name in names and name != "read_receipt"], self.scope)
        view._guards = self._guards
        return view

    def _invoke(self, action: Action, function: Callable[[Action], Outcome]) -> Outcome:
        """No effect until start is durable; detach execution inputs and outcomes."""
        self.store.started(action)
        encoded = canonical(action)
        dispatched = Action.model_validate_json(encoded)
        start = time.monotonic()
        try:
            candidate = function(dispatched)
            if not isinstance(candidate, Outcome):
                raise Conflict("executor returned an invalid outcome contract")
            outcome = Outcome.model_validate_json(canonical(candidate))
        except ToolFailure as error:
            outcome = Outcome(status="failed", data={"error": str(error),
                                                      "failure_details": error.details})
        except (FileNotFoundError, UnicodeError) as error:
            outcome = Outcome(status="failed", data={"error": str(error)})
        except Exception as error:
            outcome = Outcome(status="unknown", data={"error_type": type(error).__name__})
        except BaseException as error:
            self.store.settle(action, Outcome(status="unknown", data={
                "interrupted": True, "error_type": type(error).__name__}),
                (time.monotonic() - start) * 1000)
            raise
        try:
            changed = canonical(dispatched) != encoded
        except (ValueError, TypeError):
            changed = True
        if changed:
            outcome = Outcome(status="unknown", data={"error_type": "InputMutation"})
        try:
            outcome = Outcome.model_validate_json(canonical(outcome))
        except (ValueError, TypeError):
            outcome = Outcome(status="unknown", data={"error_type": "InvalidOutcomeSnapshot"})
        self.store.settle(action, outcome, (time.monotonic() - start) * 1000)
        return Outcome.model_validate_json(canonical(outcome))

    def internal(self, action: Action, credits: int,
                 function: Callable[[], Outcome], *, expected_policy: Policy | None = None,
                 expected_budget: ContextBudget | None = None,
                 adoption: ContextAdmission | None = None) -> Outcome:
        action = Action.model_validate_json(canonical(action))
        policy = expected_policy if expected_policy is not None else self.store.policy()
        policy = Policy.model_validate_json(canonical(policy))
        if adoption is not None:
            cached = self.store.admit(action, credits, policy, expected_budget=expected_budget,
                                      adoption=adoption)
        elif expected_budget is None:
            cached = self.store.admit(action, credits, policy)
        else:
            cached = self.store.admit(action, credits, policy, expected_budget=expected_budget)
        if cached is not None:
            return Outcome.model_validate_json(canonical(cached))
        return self._invoke(action, lambda _: function())

    def parallel_safe(self, actions: tuple[Action, ...]) -> bool:
        """Only explicitly opted-in, sealed tools may overlap within one batch."""
        if getattr(self.store, "concurrent_admission", False) is not True:
            return False
        for action in actions:
            binding = self._registry.lookup(action.tool)
            if binding is None:
                return False
            binding.verify()
            if binding.execution_mode != "parallel":
                return False
        return True

    def execute(self, action: Action) -> Outcome:
        prepared = self.prepare(action)
        return prepared() if callable(prepared) else prepared

    def prepare(self, action: Action) -> Outcome | Callable[[], Outcome]:
        """Check a call without dispatching its body or reserving its resources.

        Checks are durable and retain their ordinary credit cost. Accepted calls
        reserve only when dispatched, so abandoning a preparation holds nothing.
        A prepared call rechecks its sealed binding and policy at admission; it
        never bypasses the live ledger or changes replay identity.
        """
        return self._prepare(action, self._registry.lookup(action.tool))

    def execute_native(self, action: Action, specification: ToolSpec,
                       function: Callable[[], Outcome]) -> Outcome:
        """Admit a pinned Pi tool through the same guards, credits and receipts."""
        from kernelcat_executor.capabilities.workspace_tools import FunctionTool

        if action.tool != specification.name:
            raise Conflict("native specification differs from its sealed call")
        tool = FunctionTool(specification, lambda _: function())
        binding = ToolBinding(canonical(specification), tool.execute, tool)
        prepared = self._prepare(action, binding)
        return prepared() if callable(prepared) else prepared

    def _prepare(self, action: Action, binding: ToolBinding | None) -> Outcome | Callable[[], Outcome]:
        action = Action.model_validate_json(canonical(action))
        state = self.store.snapshot()
        task = state["tasks"].get(action.task)
        if task is None or task["scope"] != self.scope:
            raise Conflict("action task is not registered in this gateway scope")
        prior = state["actions"].get(action.id)
        if prior:
            if prior["action_digest"] != digest(action):
                raise Conflict("action ID reused")
            if not prior.get("terminal"):
                raise InDoubt(action.id)
            return Outcome.model_validate_json(canonical(prior["outcome"]))
        self.store.requested(action)
        policy = self.store.policy()
        if binding:
            binding.verify()
        gate = Action(id=action.id + "/gate/" + digest(policy), task=action.task, tool="__gate__",
                      arguments={"action_digest": digest(action), "policy_digest": digest(policy),
                                 "tool_digest": binding.fingerprint() if binding else None},
                      parents=action.parents)

        def decide() -> Outcome:
            if binding is None:
                return Outcome(data={"admit": False, "reason": "unregistered_tool"})
            if not Draft202012Validator(binding.spec().parameters).is_valid(action.arguments):
                return Outcome(data={"admit": False, "reason": "invalid_arguments"})
            decisions = self._guards.decisions(action, self.scope, policy)
            extra = {name: decision.verdict for name, decision in decisions.items()}
            denied = [(name, decision.reason) for name, decision in decisions.items() if decision.verdict == "deny"]
            reason = "; ".join(name + (": " + detail if detail else "") for name, detail in denied)[:256]
            return Outcome(data={"admit": not denied, "reason": reason or "guard",
                                 "extension_verdicts": extra})

        try:
            decision = self.internal(gate, 1, decide, expected_policy=policy)
        except BudgetExceeded:
            return self.store.reject(action, "budget", policy)
        if decision.status != "completed":
            raise InDoubt("gate outcome unresolved")
        if not decision.data["admit"]:
            return self.store.reject(action, decision.data["reason"], policy)
        if digest(self.store.policy()) != digest(policy):
            raise Conflict("policy changed during guard evaluation; retry under a fresh gate")
        assert binding is not None
        return lambda: self._execute_prepared(action, policy, binding)

    def _execute_prepared(self, action: Action, policy: Policy, binding: ToolBinding) -> Outcome:
        binding.verify()
        try:
            quote = binding.resources()
            if quote:
                if not isinstance(self.store, ResourceStore):
                    raise Conflict("resource tool requires a resource-aware journal")
                cached = self.store.admit_resources(action, binding.spec().credits, policy, quote)
            else:
                cached = self.store.admit(action, binding.spec().credits, policy)
        except BudgetExceeded:
            return self.store.reject(action, "budget", policy)
        if cached is not None:
            return Outcome.model_validate_json(canonical(cached))
        return self._invoke(action, binding.invoke)
