"""Scoped, sealed capability snapshots; deliberately not a dynamic plugin system."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol, cast

from jsonschema import Draft202012Validator
from pydantic import Field

from kernelcat_executor.contracts.models import (
    Action,
    Conflict,
    Contract,
    Outcome,
    Policy,
    ToolSpec,
    canonical,
    digest,
)
from kernelcat_executor.contracts.ports import ResourceTool, Tool
from kernelcat_executor.contracts.resources import resources


class GateDecision(Contract):
    """An extension can narrow permission, never grant it or rewrite identity."""

    verdict: Literal["deny", "abstain"]
    reason: str = Field(default="", max_length=256)


class ExecutionGuard(Protocol):
    @property
    def name(self) -> str: ...

    def check(self, action: Action, scope: str, policy: Policy) -> GateDecision: ...


@dataclass(frozen=True)
class ToolBinding:
    specification: str
    invoke: Callable[[Action], Outcome]
    source: Tool
    resource_specification: str = "{}"
    execution_mode: Literal["sequential", "parallel"] = "sequential"

    def spec(self) -> ToolSpec:
        return ToolSpec.model_validate_json(self.specification)

    def resources(self) -> dict[str, int]:
        import json
        return resources(json.loads(self.resource_specification))

    def verify(self) -> None:
        if getattr(self.source, "execution_mode", "sequential") != self.execution_mode:
            raise Conflict("registered tool execution mode changed during the session")
        if canonical(self.source.spec) != self.specification:
            raise Conflict("registered tool schema or credit quote changed during the session")

        quote = self.source.resources if isinstance(self.source, ResourceTool) else {}
        if canonical(resources(quote)) != self.resource_specification:
            raise Conflict("registered resource bound changed during the session")

    def fingerprint(self) -> str:
        if self.execution_mode == "parallel":
            return digest({"spec": self.spec().model_dump(mode="json"),
                           "resources": self.resources(), "execution_mode": self.execution_mode})
        if self.resource_specification != "{}":
            return digest({"spec": self.spec().model_dump(mode="json"),
                           "resources": self.resources()})
        return digest(self.spec())


class ToolRegistry:
    """One instance per gateway scope. Registration ends at construction.

    Python owners remain trusted. This protects advertised schemas/costs from
    accidental drift; it is not an isolation boundary for hostile Python plugins.
    """

    def __init__(self, tools: list[Tool]) -> None:
        bindings: dict[str, ToolBinding] = {}
        for tool in tools:
            spec = ToolSpec.model_validate_json(canonical(tool.spec))
            if not spec.name or spec.name in bindings or spec.name.startswith("__"):
                raise ValueError("duplicate, empty or reserved tool name")
            Draft202012Validator.check_schema(spec.parameters)
            quote = tool.resources if isinstance(tool, ResourceTool) else {}
            mode = getattr(tool, "execution_mode", "sequential")
            if not isinstance(mode, str) or mode not in {"sequential", "parallel"}:
                raise ValueError("invalid tool execution mode")
            bindings[spec.name] = ToolBinding(canonical(spec), tool.execute, tool,
                                            canonical(resources(quote)), cast(Literal["sequential", "parallel"], mode))
        self._bindings: Mapping[str, ToolBinding] = MappingProxyType(bindings)
        self.tools: Mapping[str, Tool] = MappingProxyType({k: b.source for k, b in bindings.items()})

    def lookup(self, name: str) -> ToolBinding | None:
        return self._bindings.get(name)

    def specifications(self) -> tuple[ToolSpec, ...]:
        for binding in self._bindings.values():
            binding.verify()
        return tuple(binding.spec() for binding in self._bindings.values())


class GuardChain:
    """Deterministic deny/abstain chain. Each check receives detached inputs."""

    def __init__(self, guards: tuple[ExecutionGuard, ...] = ()) -> None:
        names = [g.name for g in guards]
        if any(not n or n.startswith("__") for n in names) or len(set(names)) != len(names):
            raise ValueError("guard names must be unique and nonempty")
        self._guards = tuple((g.name, g.check) for g in guards)

    def evaluate(self, action: Action, scope: str, policy: Policy) -> dict[str, str]:
        action_json, policy_json = canonical(action), canonical(policy)
        decisions: dict[str, str] = {}
        for name, check in self._guards:
            proposed = Action.model_validate_json(action_json)
            frozen_policy = Policy.model_validate_json(policy_json)
            answer = check(proposed, scope, frozen_policy)
            if canonical(proposed) != action_json or canonical(frozen_policy) != policy_json:
                raise Conflict("admission guard mutated its execution or policy snapshot")
            if not isinstance(answer, GateDecision):
                raise Conflict("admission guard did not return a typed deny/abstain decision")
            decisions[name] = GateDecision.model_validate_json(canonical(answer)).verdict
        return decisions
