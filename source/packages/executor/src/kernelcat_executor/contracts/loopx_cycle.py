"""Operator-owned bounded LoopX Turn plan, not an agent-authored configuration."""
from __future__ import annotations

import json
from typing import Any, Literal, cast

from jsonschema import Draft202012Validator
from pydantic import Field, model_validator

from kernelcat_executor.contracts.base import Contract
from kernelcat_executor.contracts.json_snapshot import validated_snapshot
from kernelcat_executor.contracts.loopx import LoopXBinding
from kernelcat_executor.contracts.models import Json, canonical
from kernelcat_executor.generated.harness import SCHEMA, LoopXHostOpenRequest

LOOPX_HOST_METHODS = frozenset({"loopx_open", "loopx_finish", "loopx_branch"})


def read_host_open(value: object) -> Json:
    checked = cast(LoopXHostOpenRequest, validated_snapshot(value, Draft202012Validator(
        {**SCHEMA, "$ref": "#/$defs/LoopXHostOpenRequest"}), "LoopX host open"))
    # Preserve the original JSON bytes until the authoritative reader rejects
    # duplicate keys; a Node JSON parse must not silently discard them.
    return read_request(checked["request_json"].encode("utf-8"))


class LoopXCycle(Contract):
    schema_version: Literal["kernelcat.loopx-cycle/v1"] = "kernelcat.loopx-cycle/v1"
    binding: LoopXBinding
    config_path: str = Field(min_length=1)
    task_path: str = Field(min_length=1)
    validator_argv: tuple[str, ...] = Field(min_length=1, max_length=32)
    validator_timeout: float = Field(gt=0, le=120, default=30)
    max_turns: int = Field(ge=1, le=20, default=1)
    turn_steps: int = Field(ge=1, le=32, default=8)
    turn_timeout: float = Field(gt=0, le=1800, default=120)
    max_seconds: float = Field(gt=0, le=3600, default=600)
    completion: bool = False
    allow_evaluation: bool = False
    round_mode: Literal["experiment", "bounded"] = "experiment"

    @model_validator(mode="after")
    def bounded_commands(self) -> LoopXCycle:
        if any(not item or "\0" in item or len(item) > 1024 for item in self.validator_argv):
            raise ValueError("validator argv contains an empty, NUL or oversized argument")
        if self.turn_timeout + self.validator_timeout + 15 > self.max_seconds:
            raise ValueError("max_seconds must include host, validator and settlement allowance")
        return self


def read_request(raw: bytes) -> Json:
    def pairs(items: list[tuple[str, Any]]) -> Json:
        result: Json = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate host request key")
            result[key] = value
        return result
    if len(raw) > 1048576:
        raise ValueError("host request exceeds byte limit")
    result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    canonical(result)
    if not isinstance(result, dict):
        raise ValueError("host request must be an object")
    return cast(Json, result)

