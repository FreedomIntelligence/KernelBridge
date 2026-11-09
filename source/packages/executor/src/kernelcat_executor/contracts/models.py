"""Versioned, strict contracts. No I/O and no vendor types belong here."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from kernelcat_executor.contracts.base import Contract as Contract

Json = dict[str, Any]


def canonical(value: Any) -> str:
    if isinstance(value, BaseModel):
        # JSON mode can silently convert NaN to null. Preserve Python values
        # until the strict encoder has checked them.
        value = value.model_dump(mode="python")
    _validate_json(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _validate_json(value: Any, depth: int = 0) -> None:
    """Reject lossy key coercion, non-JSON objects and pathological/cyclic trees."""
    if depth > 128:
        raise ValueError("JSON nesting exceeds 128 levels")
    if value is None or type(value) in {str, bool, int, float}:
        return  # json.dumps(allow_nan=False) enforces finite floats.
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        for item in value.values():
            _validate_json(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_json(item, depth + 1)
    else:
        raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def same_json_value(left: Any, right: Any) -> bool:
    """Compare a wire round trip without rewriting durable bytes or digests.

    JavaScript serializes 0.0 as 0. JSON has one number type; booleans and
    strings remain distinct, and lossy large-integer rounding is rejected.
    """
    if type(left) in (int, float) and type(right) in (int, float):
        return (not isinstance(left, float) or math.isfinite(left)) and (
            not isinstance(right, float) or math.isfinite(right)) and left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_json_value(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_json_value(a, b) for a, b in zip(left, right))
    return type(left) in (str, bool, type(None)) and left == right


class Action(Contract):
    id: str = Field(min_length=1)
    task: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: Json = Field(default_factory=dict)
    parents: tuple[str, ...] = ()


class Outcome(Contract):
    status: Literal["completed", "failed", "unknown"] = "completed"
    data: Json = Field(default_factory=dict)






class Policy(Contract):
    schema_version: Literal["kernelcat.execution-policy/v2"] = "kernelcat.execution-policy/v2"
    revision: int = Field(ge=0, default=0)
    routes: tuple[str, ...] = ("primary",)
    max_steps: int = Field(gt=0, le=10000, default=32)
    history_items: int = Field(gt=0, le=10000, default=32)
    context_bytes: int = Field(ge=4096, le=2097152, default=65536)
    observation_bytes: int = Field(ge=512, le=65536, default=8192)

    @field_validator("routes")
    @classmethod
    def valid_routes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not route.strip() for route in value) or len(set(value)) != len(value):
            raise ValueError("routes must be nonempty and unique")
        return value




class Reply(Contract):
    tool: str
    arguments: Json
    usage: Json = Field(default_factory=dict)


class Task(Contract):
    id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    contract: Json = Field(default_factory=dict)
    seed_files: dict[str, str] = Field(default_factory=dict)


class ToolSpec(Contract):
    name: str
    description: str
    parameters: Json
    credits: int = Field(ge=0)


class HarnessError(Exception):
    """Expected, user-visible harness failure."""


class Conflict(HarnessError):
    pass


class BudgetExceeded(HarnessError):
    pass


class InDoubt(HarnessError):
    """The original attempt may have executed. Never silently retry it."""


class ToolFailure(HarnessError):
    """Known terminal failure, with optional safe structured metadata."""

    def __init__(self, message: str, *, details: Json | None = None) -> None:
        super().__init__(message)
        self.details = details or {}
