"""Shared finite-JSON snapshots for generated protocol boundaries."""
from __future__ import annotations

import json
import math
from typing import cast

from jsonschema.protocols import Validator


def _finite_integer(text: str) -> int:
    value = int(text)
    if not math.isfinite(value):
        raise ValueError("JSON integer exceeds the finite Node number range")
    return value


def _finite_float(text: str) -> int | float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("JSON number exceeds the finite Node number range")
    # JSON Schema integers include 1.0; generated Python integer fields use int.
    return int(value) if value.is_integer() else value


def validated_snapshot(value: object, validator: Validator, label: str) -> object:
    try:
        encoded = json.dumps(value, allow_nan=False)
        # Node decodes all JSON numbers as doubles; Python integers must also fit.
        snapshot = json.loads(encoded, parse_int=_finite_integer, parse_float=_finite_float)
    except (TypeError, ValueError, OverflowError) as json_error:
        raise ValueError(f"{label} must contain finite JSON values") from json_error
    error = next(validator.iter_errors(snapshot), None)
    if error is not None:
        location = "/" + "/".join(map(str, error.absolute_path))
        raise ValueError(f"Invalid {label} at {location}: {error.message}") from error
    return cast(object, snapshot)
