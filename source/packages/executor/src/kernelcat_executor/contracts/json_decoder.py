"""Strict object decoding shared by finite JSON boundaries; no transport I/O."""
from __future__ import annotations

import json
from typing import Any


class WorkerError(ValueError):
    """A transport failure; never an instruction to retry an external effect."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkerError("duplicate JSON key")
        result[key] = value
    return result


def decode_object(raw: bytes) -> dict[str, Any]:
    """Decode an object with strict UTF-8, unique keys and no NaN constants."""
    def invalid_constant(value: str) -> None:
        raise WorkerError(f"non-finite JSON constant: {value}")

    try:
        request = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                             parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise WorkerError("invalid worker JSON") from error
    if not isinstance(request, dict):
        raise WorkerError("worker JSON must be an object")
    return request
