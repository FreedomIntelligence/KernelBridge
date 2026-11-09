"""Validated finite RSI protocol. No providers or scheduling authority."""
from __future__ import annotations

from typing import Protocol, cast

from jsonschema import Draft202012Validator

from kernelcat_executor.contracts.json_snapshot import validated_snapshot
from kernelcat_executor.contracts.models import Json
from kernelcat_executor.generated.rsi import SCHEMA


def validate_rsi(value: object, definition: str) -> Json:
    validator = Draft202012Validator({**SCHEMA, "$ref": "#/$defs/" + definition})
    return cast(Json, validated_snapshot(value, validator, definition))


def validate_policy(value: object) -> Json:
    policy = validate_rsi(value, "RsiPolicy")
    if policy["delta"] <= policy["epsilon"]:
        raise ValueError("RSI delta must exceed epsilon to preserve hysteresis")
    return policy


class RsiSession(Protocol):
    """An explicitly bound finite capability; Node chooses the next operation."""

    def status(self, step: int, stop: bool) -> Json: ...
    def context(self, step: int) -> list[Json]: ...
    def handle(self, method: str, request: Json, step: int) -> Json: ...
    def check_finished(self) -> None: ...
