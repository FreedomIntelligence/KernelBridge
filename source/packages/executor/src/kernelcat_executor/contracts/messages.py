"""Provider-neutral, versioned assistant turns; execution identity stays host-owned."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from kernelcat_executor.contracts.base import Contract
from kernelcat_executor.contracts.models import Action, Json, canonical

TURN_SCHEMA: Literal["kernelcat.assistant-turn/v1"] = "kernelcat.assistant-turn/v1"


class TextContent(Contract):
    type: Literal["text"] = "text"
    text: str


class ToolCall(Contract):
    type: Literal["toolCall"] = "toolCall"
    id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=256)
    arguments: Json = Field(default_factory=dict)

    @model_validator(mode="after")
    def finite_arguments(self) -> ToolCall:
        canonical(self.arguments)
        if not self.id.strip() or not self.name.strip():
            raise ValueError("tool identifiers must not be blank")
        return self


Content = Annotated[TextContent | ToolCall, Field(discriminator="type")]


class ToolBatchPlan(Contract):
    """A host-authored scheduling decision, not part of the provider response."""

    schema_version: Literal["kernelcat.tool-batch/v1"] = "kernelcat.tool-batch/v1"
    execution_mode: Literal["parallel"] = "parallel"
    max_workers: int = Field(ge=1, le=32)


class AssistantTurn(Contract):
    schema_version: Literal["kernelcat.assistant-turn/v1"] = TURN_SCHEMA
    content: tuple[Content, ...] = Field(min_length=1, max_length=128)
    stop_reason: Literal["stop", "tool_calls"]
    usage: Json = Field(default_factory=dict)

    @model_validator(mode="after")
    def coherent_turn(self) -> AssistantTurn:
        calls = self.tool_calls
        if len(calls) > 64 or len({call.id for call in calls}) != len(calls):
            raise ValueError("tool calls must have unique IDs and at most 64 entries")
        if bool(calls) != (self.stop_reason == "tool_calls"):
            raise ValueError("stop reason does not match the assistant content")
        if not calls and not self.text.strip():
            raise ValueError("an empty response is not a completed turn")
        canonical(self.usage)
        return self

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(block for block in self.content if isinstance(block, ToolCall))

    @property
    def text(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextContent))


def turn_actions(turn: AssistantTurn, task_id: str, proposal_id: str, *,
                 parallel: bool = False) -> tuple[Action, ...]:
    """Never use a provider's tool-call ID as an action or filesystem identity.

    Legacy calls form a source-order receipt chain. Parallel calls depend on the
    host-authored batch plan instead; their completion order is not a dependency.
    """
    frozen = AssistantTurn.model_validate_json(canonical(turn))
    actions: list[Action] = []
    parents: tuple[str, ...]
    for index, call in enumerate(frozen.tool_calls):
        if parallel:
            parents = (proposal_id, proposal_id + "/batch")
        else:
            parents = (proposal_id,) if not actions else (proposal_id, actions[-1].id)
        actions.append(Action(id=f"{proposal_id}/call/{index}", task=task_id,
                              tool=call.name, arguments=call.arguments, parents=parents))
    return tuple(actions)
