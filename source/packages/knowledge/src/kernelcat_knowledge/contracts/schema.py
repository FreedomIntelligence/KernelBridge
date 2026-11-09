"""Pure JSON and schema validation, shared with the standalone startup bundle."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any


class SchemaError(ValueError):
    """A value violates the generated schema or canonical JSON domain."""

MAX_SAFE_INTEGER = 2**53 - 1


def _wire_value(value: Any, ancestors: set[int], *, allow_floats: bool = False) -> None:
    if value is None or type(value) is bool:
        return
    if type(value) is int and abs(value) <= MAX_SAFE_INTEGER:
        return
    if allow_floats and type(value) is float and math.isfinite(value):
        return
    if type(value) is str:
        value.encode("utf-8", errors="strict")
        return
    if type(value) not in {dict, list} or id(value) in ancestors:
        raise SchemaError(
            "runtime JSON requires acyclic primitives and safe integers"
        )
    ancestors.add(id(value))
    try:
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise SchemaError("runtime JSON object keys must be strings")
                _wire_value(key, ancestors, allow_floats=allow_floats)
                _wire_value(item, ancestors, allow_floats=allow_floats)
        else:
            for item in value:
                _wire_value(item, ancestors, allow_floats=allow_floats)
    finally:
        ancestors.remove(id(value))


def wire_bytes(value: Any, *, allow_floats: bool = False) -> bytes:
    try:
        _wire_value(value, set(), allow_floats=allow_floats)
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (UnicodeError, RecursionError) as exc:
        raise SchemaError("runtime JSON contains invalid Unicode or nesting") from exc


def wire_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(wire_bytes(value)).hexdigest()


def _validate(
    value: Any, schema: dict[str, Any], path: str, definitions: dict[str, Any]
) -> None:
    if "$ref" in schema:
        _validate(value, definitions[schema["$ref"].split("/")[-1]], path, definitions)
    if "const" in schema and (
        value != schema["const"] or type(value) is not type(schema["const"])
    ):
        raise SchemaError(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: invalid enum value")
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    kind = schema.get("type")
    if kind == "number":
        valid_type = type(value) in {int, float}
    elif kind == "integer":
        valid_type = type(value) is int or (type(value) is float and value.is_integer())
    else:
        valid_type = kind is None or type(value) is types[kind]
    if not valid_type:
        raise SchemaError(f"{path}: expected {kind}")
    if type(value) is dict:
        _validate_object(value, schema, path, definitions)
    elif type(value) is list:
        _validate_array(value, schema, path, definitions)
    elif type(value) in {str, int, float}:
        _validate_scalar(value, schema, path)
    for option in schema.get("allOf", []):
        _validate_conditional(value, option, path, definitions)
    if "oneOf" in schema:
        matches = 0
        for option in schema["oneOf"]:
            try:
                _validate(value, option, path, definitions)
                matches += 1
            except SchemaError:
                pass
        if matches != 1:
            raise SchemaError(f"{path}: expected exactly one schema alternative")


def _validate_object(
    value: dict[str, Any], schema: dict[str, Any], path: str, definitions: dict[str, Any]
) -> None:
    if set(schema.get("required", [])) - value.keys():
        raise SchemaError(f"{path}: missing required fields")
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False and value.keys() - properties.keys():
        raise SchemaError(f"{path}: unknown fields")
    for key, item in value.items():
        if key in properties:
            _validate(item, properties[key], f"{path}.{key}", definitions)


def _validate_array(
    value: list[Any], schema: dict[str, Any], path: str, definitions: dict[str, Any]
) -> None:
    if len(value) < schema.get("minItems", 0):
        raise SchemaError(f"{path}: too few items")
    if len(value) > schema.get("maxItems", MAX_SAFE_INTEGER):
        raise SchemaError(f"{path}: too many items")
    if "items" in schema:
        for item in value:
            _validate(item, schema["items"], f"{path}[]", definitions)
    if schema.get("uniqueItems") and len({wire_bytes(item) for item in value}) != len(
        value
    ):
        raise SchemaError(f"{path}: duplicate items")


def _validate_scalar(value: Any, schema: dict[str, Any], path: str) -> None:
    if type(value) is str:
        if (
            not schema.get("minLength", 0)
            <= len(value)
            <= schema.get("maxLength", MAX_SAFE_INTEGER)
        ):
            raise SchemaError(f"{path}: string length outside bounds")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise SchemaError(f"{path}: invalid string format")
    elif (
        not schema.get("minimum", -MAX_SAFE_INTEGER)
        <= value
        <= schema.get("maximum", MAX_SAFE_INTEGER)
    ):
        raise SchemaError(f"{path}: integer outside bounds")


def _validate_conditional(
    value: Any, schema: dict[str, Any], path: str, definitions: dict[str, Any]
) -> None:
    if "if" not in schema:
        _validate(value, schema, path, definitions)
        return
    try:
        _validate(value, schema["if"], path, definitions)
    except SchemaError:
        _validate(value, schema.get("else", {}), path, definitions)
    else:
        _validate(value, schema.get("then", {}), path, definitions)


def validate_definition(value: Any, schema: dict[str, Any], definition: str) -> None:
    """Validate a generated local definition; owners still enforce semantic rules."""
    _validate(value, schema["$defs"][definition], "$", schema["$defs"])
