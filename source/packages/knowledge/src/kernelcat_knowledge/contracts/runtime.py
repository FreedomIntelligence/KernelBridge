"""Validate runtime contracts while preserving the public ContractError boundary."""
from __future__ import annotations

import json
from typing import Any, TypeVar, cast

from kernelcat_knowledge.contracts import ContractError
from kernelcat_knowledge.contracts import schema as _schema
from kernelcat_knowledge.generated.knowledge_runtime import SCHEMA, KnowledgeDependency

T = TypeVar("T")
MAX_SAFE_INTEGER = _schema.MAX_SAFE_INTEGER


def wire_bytes(value: Any, *, allow_floats: bool = False) -> bytes:
    try:
        return _schema.wire_bytes(value, allow_floats=allow_floats)
    except _schema.SchemaError as error:
        raise ContractError(str(error)) from error


def wire_digest(value: Any) -> str:
    try:
        return _schema.wire_digest(value)
    except _schema.SchemaError as error:
        raise ContractError(str(error)) from error


def validate_definition(value: Any, schema: dict[str, Any], definition: str) -> None:
    try:
        _schema.validate_definition(value, schema, definition)
    except _schema.SchemaError as error:
        raise ContractError(str(error)) from error


def validate_runtime(value: T, definition: str) -> T:
    """Return an independent JSON value after validating the generated definition."""
    # Refuse oversized strings/arrays before copying their full wire payload.
    validate_definition(value, SCHEMA, definition)
    copied = json.loads(wire_bytes(value))
    return cast(T, copied)


def dependency_key(value: KnowledgeDependency) -> tuple[bytes, bytes, bytes, bytes]:
    return (
        value["kind"].encode("utf-8"),
        value["object_id"].encode("utf-8"),
        (value["revision_id"] or "").encode("utf-8"),
        value["content_digest"].encode("utf-8"),
    )
