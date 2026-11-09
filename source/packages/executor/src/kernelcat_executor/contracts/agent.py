"""Public lifecycle values. Events describe execution; they never authorize it."""
from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import Field, model_validator

from kernelcat_executor.contracts.base import Contract
from kernelcat_executor.contracts.models import Json, canonical

QueueMode = Literal["one-at-a-time", "all"]
InputKind = Literal["prompt", "steer", "follow_up"]
EventKind = Literal[
    "agent_start", "agent_end", "turn_start", "turn_end",
    "message_start", "message_end", "tool_execution_start", "tool_execution_end",
]


class AgentOptions(Contract):
    tool_execution: Literal["sequential", "parallel"] = "sequential"
    max_parallel_tools: int = Field(ge=1, le=32, default=4)
    steering_mode: QueueMode = "one-at-a-time"
    follow_up_mode: QueueMode = "one-at-a-time"


class AgentEvent(Contract):
    type: EventKind
    task_id: str
    run_id: str
    sequence: int = Field(ge=0)
    step: int | None = None
    data: Json = Field(default_factory=dict)


class AgentInput(Contract):
    kind: InputKind
    text: str = Field(min_length=1, max_length=65536)

    @model_validator(mode="after")
    def bounded_text(self) -> AgentInput:
        if not self.text.strip() or len(self.text.encode("utf-8")) > 65536:
            raise ValueError("input must be nonblank and at most 64 KiB")
        return self


class AgentState(Contract):
    status: Literal["idle", "running", "closed"]
    task_id: str | None = None
    messages: tuple[Json, ...] = ()
    pending_tool_calls: tuple[str, ...] = ()
    error: str | None = None

    @model_validator(mode="after")
    def finite_messages(self) -> AgentState:
        canonical(self.messages)
        return self


Listener = Callable[[AgentEvent], object]
