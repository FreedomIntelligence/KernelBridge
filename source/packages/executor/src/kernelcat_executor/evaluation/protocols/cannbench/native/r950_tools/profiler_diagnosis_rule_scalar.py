#!/usr/bin/env python3
"""Profiler diagnosis rule: scalar."""
from __future__ import annotations

from typing import Any, Mapping

from profiler_diagnosis_rule_core import (
    _RuleResult, _check, _compact_number, _delta,
    _extreme_ratio, _metric_rows, _policy,
)

def _scalar_rule(base: Mapping[str, Any], candidate: Mapping[str, Any], relative: Mapping[str, Any]) -> _RuleResult | None:
    field, scalar = _extreme_ratio(candidate, (r"scalar.*ratio", r"scalar.*time"))
    _, vec = _extreme_ratio(candidate, (r"(?:aiv_)?vec.*ratio",))
    _, mte2 = _extreme_ratio(candidate, (r"mte2.*ratio",))
    if scalar is None or scalar < 0.25 or scalar < max(vec or 0.0, mte2 or 0.0) * 0.7:
        return None
    return _RuleResult(
        "scalar_control_pressure", "Scalar/control and address-generation pressure", "high", min(1.0, scalar * 1.5),
        "Runtime scalar work is large relative to Vector/Cube/MTE activity.",
        _metric_rows(base, candidate, relative, (r"scalar", r"(?:aiv_)?vec.*ratio", r"mte2.*ratio")),
        {"dominant_field": field, "scalar_ratio": _compact_number(scalar, 4)},
        ("inner-loop offset/index arithmetic", "dynamic tiling reads or GetValue calls", "complex branches/tail predicates", "high-level API initialization or AIC/AIV message handling"),
        (
            _policy("hoist_scalar_invariants", "Move invariant address/tiling work out of hot loops", preconditions=("expressions are invariant over the loop/tile", "hoisting preserves overflow and tail semantics"), code_search=("for", "offset", "index", "tilingData", "GetValue", "if"), expected=("scalar ratio/time decreases", "first-MTE2 head gap decreases", "Event latency decreases"), fallback="keep dynamic path for unsupported shape/domain"),
            _policy("constantize_tiling_parameters", "Make compile-time-known tiling values constants", preconditions=("value is fixed by TilingKey/capability domain", "route identity proves the specialized path"), code_search=("tilingData->", "GetValue", "template parameter", "TilingKey"), expected=("scalar head overhead decreases", "Event latency decreases"), fallback="retain generic dynamic parent path"),
        ),
        (_delta(r"scalar.*ratio|scalar.*time", "decrease", "less runtime control work"), _delta(r"(?:aiv_)?vec.*ratio|(?:mac|cube).*ratio", "increase_relative", "more time reaches useful compute")),
        (_check("scalar_timeline", "Is scalar time concentrated before the first MTE2 or in every tile?", ("timeline", "first-MTE2 timestamp", "loop trip count")), _check("invariant_audit", "Which index/tiling expressions are invariant in the proven route?", ("source review", "route/TilingKey", "shape domain"))),
        ("A scalar ratio may include framework/API setup that is not in the obvious kernel loop",),
    )
