"""Integer resource units, independent of credits and provider implementation.

A quote is an enforced upper bound, not a prediction. Trusted tools enforce the
bound and may report lower actual usage. Missing usage charges the entire quote.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

UNITS = frozenset({"requests", "bytes", "tokens", "cpu_ms", "device_ms", "micro_usd"})


def resources(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping) or any(key not in UNITS for key in value):
        raise ValueError("unknown resource unit or invalid resource mapping")
    if any(type(n) is not int or n < 0 or n > 10**18 for n in value.values()):
        raise ValueError("resources require bounded nonnegative integer amounts")
    return {key: value[key] for key in sorted(value)}
