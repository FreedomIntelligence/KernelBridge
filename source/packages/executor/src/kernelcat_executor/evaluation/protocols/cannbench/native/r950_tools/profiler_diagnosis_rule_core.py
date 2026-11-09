#!/usr/bin/env python3
"""Bottleneck signatures and candidate policies for 910C profiler diagnosis.

The module deliberately maps counters to *suspects*, not conclusions. Every
candidate policy is non-automatic, carries explicit preconditions/fallback,
and is gated by profiler-off Event timing plus correctness.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


DIAGNOSIS_SCHEMA_VERSION = "ascend-profiler-diagnosis/v1"
COMPACT_DIAGNOSIS_SCHEMA_VERSION = "ascend-profiler-diagnosis-compact/v1"
AUTHORITY_CONTRACT = {
    "latency_reward": "profiler-off NPU Event",
    "pmu_role": "mechanism attribution only",
    "profiler_duration_is_reward": False,
    "promotion_rule": "stable Event win with LCB95 > 1 and unchanged correctness",
    "code_attribution_rule": "verified route-hit and verified source-change evidence required",
}
_EVENT_GATE = {
    "authority": "profiler-off NPU Event",
    "required": (
        "correctness complete and unchanged",
        "timing pair stable",
        "candidate speedup greater than 1",
        "speedup 95% lower confidence bound greater than 1",
    ),
    "pmu_is_reward": False,
}
_GLOBAL_BLIND_SPOTS = (
    "algorithmic redundancy can keep a compute pipe busy while doing unnecessary work",
    "cross-kernel fusion intent is not recoverable from counters alone",
    "a normal-looking profile does not prove the intended route or TilingKey executed",
    "bandwidth-bound versus small-copy latency requires bytes, copy granularity, and ceiling context",
    "high utilization is a symptom, not proof of useful work",
)


@dataclass(frozen=True)
class _RuleResult:
    rule_id: str
    title: str
    confidence: str
    score: float
    mechanism: str
    matched: tuple[dict[str, Any], ...]
    derived: Mapping[str, Any]
    suspect_code: tuple[str, ...]
    candidate_policies: tuple[dict[str, Any], ...]
    expected_pmu_delta: tuple[dict[str, Any], ...]
    disambiguation_checks: tuple[dict[str, Any], ...]
    blind_spots: tuple[str, ...]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value.endswith("%"):
            value = value[:-1].strip()
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _compact_number(value: Any, digits: int = 6) -> float | int | None:
    number = _number(value)
    if number is None:
        return None
    rounded = round(number, digits)
    if rounded == 0:
        rounded = 0.0
    if float(rounded).is_integer() and abs(rounded) < 10**12:
        return int(rounded)
    return rounded


def _case_index(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    match = re.search(r"(\d+)$", str(value or ""))
    return int(match.group(1)) if match else None


def _canonical(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _ratio(value: Any) -> float | None:
    """Normalize common counter encodings to a fraction when unambiguous."""
    number = _number(value)
    if number is None or number < 0:
        return None
    if number <= 1.5:
        return number
    if number <= 100.0:
        return number / 100.0
    return None


def _matches(field: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, field, flags=re.IGNORECASE) for pattern in patterns)


def _metric_rows(
    base: Mapping[str, Any],
    candidate: Mapping[str, Any],
    relative: Mapping[str, Any],
    patterns: Iterable[str],
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for field in sorted(set(base) | set(candidate)):
        if not _matches(field, patterns):
            continue
        row: dict[str, Any] = {"field": field}
        for key, source, digits in (
            ("base", base, 6),
            ("candidate", candidate, 6),
            ("relative_delta", relative, 4),
        ):
            value = _compact_number(source.get(field), digits)
            if value is not None:
                row[key] = value
        rows.append(row)
    return tuple(rows)


def _extreme_ratio(
    metrics: Mapping[str, Any], patterns: Iterable[str], *, minimum: bool = False
) -> tuple[str | None, float | None]:
    best_field: str | None = None
    best_value: float | None = None
    for field, raw in metrics.items():
        if not _matches(field, patterns):
            continue
        value = _ratio(raw)
        if value is None:
            continue
        if best_value is None or (value < best_value if minimum else value > best_value):
            best_field, best_value = field, value
    return best_field, best_value


def _policy(
    policy_id: str,
    title: str,
    *,
    preconditions: Sequence[str],
    code_search: Sequence[str],
    expected: Sequence[str],
    fallback: str,
) -> dict[str, Any]:
    return {
        "id": policy_id,
        "title": title,
        "automatic": False,
        "preconditions": list(preconditions),
        "code_search": list(code_search),
        "expected_effect": list(expected),
        "event_gate": dict(_EVENT_GATE),
        "fallback": fallback,
    }


def _check(check_id: str, question: str, evidence: Sequence[str]) -> dict[str, Any]:
    return {"id": check_id, "question": question, "evidence": list(evidence)}


def _delta(pattern: str, direction: str, meaning: str) -> dict[str, str]:
    return {"field_pattern": pattern, "direction": direction, "meaning": meaning}
