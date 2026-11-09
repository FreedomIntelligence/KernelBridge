"""Dependency-inversion boundaries; application services depend on protocols."""
from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable

from kernelcat_executor.contracts.messages import AssistantTurn
from kernelcat_executor.contracts.models import Action, Json, Outcome, Policy, Reply, ToolSpec
from kernelcat_executor.contracts.profiles import AgentProfile, ProfileRevision
from kernelcat_executor.contracts.requests import ModelRequest
from kernelcat_executor.contracts.runtime import ContextAdmission
from kernelcat_executor.generated.context import ContextBudget
from kernelcat_executor.generated.harness import HarnessInput


class Proposer(Protocol):
    def propose(self, context: Json, tools: tuple[ToolSpec, ...]) -> Reply | AssistantTurn: ...


@runtime_checkable
class PreparedProposer(Protocol):
    def prepare(self, context: Json, tools: tuple[ToolSpec, ...]) -> ModelRequest: ...
    def invoke(self, request: ModelRequest) -> Reply | AssistantTurn: ...


@runtime_checkable
class HarnessProposer(Protocol):
    """Finite transport of an observed Harness input, with no prompt selection."""

    def prepare_harness(self, harness: HarnessInput) -> ModelRequest: ...
    def invoke_harness(self, request: ModelRequest) -> AssistantTurn: ...


class Tool(Protocol):
    spec: ToolSpec

    def execute(self, action: Action) -> Outcome: ...


class Store(Protocol):
    def task_lock(self, task_id: str) -> AbstractContextManager[None]: ...
    def register_task(self, task: Json) -> None: ...
    def requested(self, action: Action) -> None: ...
    def started(self, action: Action) -> None: ...
    def events(self) -> list[Json]: ...
    def snapshot(self) -> Json: ...
    def policy(self) -> Policy: ...
    def admit(self, action: Action, credits: int, policy: Policy, *,
              expected_budget: ContextBudget | None = None,
              adoption: ContextAdmission | None = None) -> Json | None: ...
    def settle(self, action: Action, outcome: Outcome, elapsed_ms: float) -> None: ...
    def reject(self, action: Action, reason: str, policy: Policy) -> Outcome: ...
    def install(self, expected: str, policy: Policy, report: Json) -> None: ...


@runtime_checkable
class ProfileStore(Protocol):
    def bind_profile(self, scope: str, profile: AgentProfile) -> None: ...
    def pin_profile(self, task_id: str) -> ProfileRevision: ...
    def current_profile(self, scope: str) -> ProfileRevision: ...
    def activate_profile(self, proposal_id: str, validation_id: str,
                         finish_id: str) -> ProfileRevision: ...


class SourceWorkspace(Protocol):
    def read(self, action: Action) -> Outcome: ...
    def write(self, action: Action) -> Outcome: ...
    def edit(self, action: Action) -> Outcome: ...


class SkillLibrary(Protocol):
    def load(self, name: str, resource: str = "SKILL.md",
             offset: int = 0, limit: int = 8192) -> Json: ...


@runtime_checkable
class ResourceTool(Protocol):
    """Optional fixed upper bounds, sealed together with the registered tool."""
    resources: dict[str, int]


@runtime_checkable
class ResourceStore(Protocol):
    def admit_resources(self, action: Action, credits: int, policy: Policy,
                        quote: dict[str, int]) -> Json | None: ...


@runtime_checkable
class ManagedStore(Protocol):
    """Optional runtime projection supplied by a trusted Actor-bound owner view."""
    def runtime_state(self) -> Json: ...
