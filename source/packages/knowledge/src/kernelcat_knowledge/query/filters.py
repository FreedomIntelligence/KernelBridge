"""Structured, alias-aware retrieval contract for Ascend operator knowledge."""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_REPRO_ORDER = {"": 0, "concept": 1, "pseudocode": 2, "api-valid": 3, "compile-tested": 4, "run-tested": 5, "benchmarked": 6}
_VERIFICATION_ORDER = {"": 0, "api": 1, "compile": 2, "run": 3, "benchmark": 4}


def normalize_term(value: Any) -> str:
    return re.sub(r"[\s_\-./]+", "", str(value or "").strip().lower())


def _list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


class AliasResolver:
    def __init__(self, path: Path | None = None, mapping: Mapping[str, Any] | None = None):
        if mapping is None and path and path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            mapping = raw.get("aliases", raw) if isinstance(raw, Mapping) else {}
        mapping = mapping or {}
        self.canonical_to_aliases: dict[str, list[str]] = {}
        self.lookup: dict[str, str] = {}
        for canonical, aliases in mapping.items():
            canonical = str(canonical)
            values = [canonical, *(str(item) for item in _list(aliases))]
            self.canonical_to_aliases[canonical] = values
            for value in values:
                self.lookup.setdefault(normalize_term(value), canonical)

    def canonicalize(self, value: Any) -> str:
        text = str(value or "").strip()
        return self.lookup.get(normalize_term(text), text.lower())

    def expand_query(self, query: str) -> str:
        expanded = [query.strip()]
        query_norm = normalize_term(query)
        for alias_norm, canonical in self.lookup.items():
            if alias_norm and alias_norm in query_norm and canonical not in expanded:
                expanded.append(canonical)
        return " ".join(item for item in expanded if item)


@dataclass(frozen=True)
class QueryFilters:
    operator: str | None = None
    direction: str | None = None
    dtype: str | None = None
    layout: str | None = None
    architecture: str | None = None
    cann: str | None = None
    reproducibility: str | None = None
    require_evidence: bool = False
    min_verification: str | None = None


def _nested(entry: Mapping[str, Any], *path: str) -> Any:
    value: Any = entry
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _values(entry: Mapping[str, Any], *paths: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for path in paths:
        for item in _list(_nested(entry, *path)):
            if not isinstance(item, Mapping):
                out.append(str(item))
    return out


def _matches(expected: str | None, values: Iterable[str], resolver: AliasResolver) -> bool:
    if not expected:
        return True
    want = normalize_term(resolver.canonicalize(expected))
    return any(normalize_term(resolver.canonicalize(value)) == want for value in values)


def verification_level(entry: Mapping[str, Any]) -> int:
    verification = entry.get("verification") or {}
    if not isinstance(verification, Mapping):
        return 0
    level = 0
    if isinstance(verification.get("api"), Mapping) and verification["api"].get("status") == "pass":
        level = 1
    if isinstance(verification.get("compile"), Mapping) and verification["compile"].get("status") == "pass":
        level = 2
    if isinstance(verification.get("correctness"), Mapping) and verification["correctness"].get("status") == "pass":
        level = 3
    benchmark = verification.get("benchmark")
    records = benchmark if isinstance(benchmark, list) else [benchmark]
    if any(isinstance(row, Mapping) and row.get("status", "pass") == "pass" for row in records):
        level = 4
    return level


def match_entry(entry: Mapping[str, Any], filters: QueryFilters, resolver: AliasResolver) -> tuple[bool, list[str]]:
    checks = (
        ("operator", filters.operator, _values(entry, ("operator",), ("task", "operator"), ("tags",))),
        ("direction", filters.direction, _values(entry, ("direction",), ("task", "direction"))),
        ("dtype", filters.dtype, _values(entry, ("dtype",), ("task", "dtype"), ("applicability", "dtype"))),
        ("layout", filters.layout, _values(entry, ("layout",), ("task", "layout"), ("applicability", "layout"))),
        ("architecture", filters.architecture, _values(entry, ("architecture",), ("architectures",), ("applicability", "architecture"), ("applicability", "soc"))),
        ("cann", filters.cann, _values(entry, ("cann",), ("applicability", "cann"))),
    )
    matched: list[str] = []
    for name, expected, values in checks:
        if expected and not _matches(expected, values, resolver):
            return False, matched
        if expected:
            matched.append(name)
    if filters.reproducibility:
        if _REPRO_ORDER.get(str(entry.get("reproducibility") or ""), 0) < _REPRO_ORDER.get(filters.reproducibility, 999):
            return False, matched
        matched.append("reproducibility")
    if filters.require_evidence and not _list(entry.get("evidence")):
        return False, matched
    if filters.require_evidence:
        matched.append("evidence")
    if filters.min_verification:
        if verification_level(entry) < _VERIFICATION_ORDER.get(filters.min_verification, 999):
            return False, matched
        matched.append("verification")
    return True, matched


def quality_bonus(entry: Mapping[str, Any]) -> float:
    bonus = 0.04 if entry.get("trust_contract") == "v1" else 0.0
    evidence = [row for row in _list(entry.get("evidence")) if isinstance(row, Mapping)]
    if evidence:
        bonus += 0.02
    if any(row.get("kind") in {"official-doc", "upstream-code", "upstream-pr"} for row in evidence):
        bonus += 0.03
    bonus += min(verification_level(entry), 4) * 0.015
    bonus += min(_REPRO_ORDER.get(str(entry.get("reproducibility") or ""), 0), 6) * 0.005
    return min(bonus, 0.15)


def estimate_tokens(value: str) -> int:
    cjk = len(re.findall(r"[\u3400-\u9fff]", value))
    return max(1, cjk + math.ceil(max(len(value) - cjk, 0) / 4))


def take_with_token_budget(items: list[dict], budget: int | None) -> tuple[list[dict], list[str], int]:
    if budget is None or budget <= 0:
        return items, [], sum(estimate_tokens(str(item)) for item in items)
    selected: list[dict] = []
    omitted: list[str] = []
    used = 0
    for item in items:
        cost = estimate_tokens(str(item))
        if selected and used + cost > budget:
            omitted.append(str(item.get("id") or item.get("ID") or "?"))
            continue
        selected.append(item)
        used += cost
    return selected, omitted, used
