"""Canonical JSON helpers shared by KernelCat and KernelCat-Wiki contracts."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


class CanonicalizationError(ValueError):
    """Raised when a value cannot participate in a stable contract digest."""


def _validate_json_value(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, int, bool)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError(f"{path}: non-finite float is not allowed")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"{path}: object keys must be strings")
            _validate_json_value(item, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    raise CanonicalizationError(f"{path}: unsupported JSON value {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically across OSes and Python processes."""
    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_digest(value: Any) -> str:
    payload = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def content_revision_id(value: Any) -> str:
    """Return a deterministic revision identity for immutable card contents."""
    return "rev:" + sha256_digest(value)
