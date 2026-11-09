"""Generated context validation and pure acceptance of selected input."""
from __future__ import annotations

from typing import cast

from jsonschema import Draft202012Validator

from kernelcat_executor.contracts.json_snapshot import validated_snapshot
from kernelcat_executor.contracts.models import Conflict, HarnessError, Json, Policy, canonical
from kernelcat_executor.contracts.runtime import SCHEMA_REGISTRY
from kernelcat_executor.generated.context import (
    SCHEMA,
    ContextSource,
    JsonValue,
    ModelContextRequest,
)

_SOURCE = Draft202012Validator(SCHEMA, registry=SCHEMA_REGISTRY)
_REQUEST = Draft202012Validator({**SCHEMA, "$ref": "#/$defs/ModelContextRequest"}, registry=SCHEMA_REGISTRY)
_VALUE = Draft202012Validator({**SCHEMA, "$ref": "#/$defs/JsonValue"})


def validate_context_value(value: object, label: str) -> JsonValue:
    """Apply the declared Node wire capability without changing Python contracts."""
    try:
        return cast(JsonValue, validated_snapshot(value, _VALUE, label))
    except ValueError as error:
        raise ValueError(
            f"Node wire {label} requires finite JSON and safe integer-valued numbers"
        ) from error


def validate_context_source(value: object) -> ContextSource:
    return cast(ContextSource, validated_snapshot(value, _SOURCE, "ContextSource"))


def validate_model_context_request(value: object) -> ModelContextRequest:
    return cast(ModelContextRequest, validated_snapshot(value, _REQUEST, "ModelContextRequest"))


class ContextOverflow(HarnessError):
    """Essential contract alone cannot fit; stop before issuing a paid model request."""


def _size(value: object) -> int:
    return len(canonical(value).encode("utf-8"))


def _same_json(expected: object, submitted: object) -> bool:
    """JSON numbers compare by value while booleans retain their distinct type."""
    if type(expected) in {int, float} and type(submitted) in {int, float}:
        return bool(expected == submitted)
    if type(expected) is not type(submitted):
        return False
    if isinstance(expected, dict) and isinstance(submitted, dict):
        return expected.keys() == submitted.keys() and all(
            _same_json(value, submitted[key]) for key, value in expected.items())
    if isinstance(expected, list) and isinstance(submitted, list):
        return len(expected) == len(submitted) and all(
            _same_json(left, right) for left, right in zip(expected, submitted, strict=True))
    return bool(expected == submitted)


def _validate_suffix(source_value: list[Json], submitted: object, field: str, *,
                     minimum: int = 0) -> int:
    if not isinstance(submitted, list) or not minimum <= len(submitted) <= len(source_value):
        raise Conflict(f"submitted context has invalid {field} candidates")
    offset = len(source_value) - len(submitted)
    if not _same_json(source_value[offset:], submitted):
        raise Conflict(f"submitted context changed a protected {field} exchange")
    return offset


def validate_submission(admitted: Json, submitted: Json, policy: Policy) -> Json:
    """Check an externally compiled prompt against its admitted immutable source."""
    if set(submitted) != set(admitted):
        raise Conflict("submitted context fields differ from the admitted source")
    variable = {"history", "inputs", "turns", "projection"}
    for key in admitted.keys() - variable:
        if not _same_json(admitted[key], submitted[key]):
            raise Conflict(f"submitted context changed protected {key}")
    omitted = _validate_suffix(admitted["history"], submitted["history"], "history")
    projection = dict(admitted["projection"])
    projection["history_omitted"] += omitted
    if "inputs" in admitted:
        _validate_suffix(admitted["inputs"], submitted["inputs"], "inputs", minimum=1)
    if "turns" in admitted:
        projection["turns_omitted"] += _validate_suffix(
            admitted["turns"], submitted["turns"], "turns")
    if not _same_json(projection, submitted["projection"]):
        raise Conflict("submitted context changed protected projection facts")
    if _size(submitted) > policy.context_bytes:
        raise ContextOverflow("submitted context exceeds context_bytes")
    return submitted
