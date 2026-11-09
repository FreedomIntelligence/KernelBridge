"""Explicit LoopX binding; control-plane identity is operator-owned, never model output."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from kernelcat_executor.contracts.base import Contract


class LoopXBinding(Contract):
    schema_version: Literal["kernelcat.loopx-binding/v1"] = "kernelcat.loopx-binding/v1"
    cli: str = Field(min_length=1)
    node: str = Field(min_length=1)
    project: str = Field(min_length=1)
    registry: str = Field(min_length=1)
    runtime_root: str = Field(min_length=1)
    home: str = Field(min_length=1)
    goal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    agent_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    todo_id: str = Field(pattern=r"^todo_[A-Za-z0-9_-]{1,128}$")
    task_id: str = Field(min_length=1)
    segment_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    version: Literal["1.2.3"] = "1.2.3"
    timeout: float = Field(gt=0, le=120, default=30)
    max_output_bytes: int = Field(ge=4096, le=16777216, default=4194304)
