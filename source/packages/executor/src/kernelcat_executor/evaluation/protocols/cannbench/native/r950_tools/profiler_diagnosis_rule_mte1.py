#!/usr/bin/env python3
"""Profiler diagnosis rule: mte1."""
from __future__ import annotations

from typing import Any, Mapping

from profiler_diagnosis_rule_core import (
    _RuleResult, _check, _compact_number, _delta,
    _extreme_ratio, _metric_rows, _policy,
)

def _mte1_rule(base: Mapping[str, Any], candidate: Mapping[str, Any], relative: Mapping[str, Any]) -> _RuleResult | None:
    field, ratio = _extreme_ratio(candidate, (r"(?:^|[._])(?:aic_)?mte1(?:_ratio)?$", r"mte1.*ratio", r"l[01].*bandwidth"))
    if ratio is None or ratio < 0.35:
        return None
    return _RuleResult(
        "l1_l0_transfer_pressure", "L1/L0 transfer pressure", "high" if ratio >= 0.6 else "medium", min(1.0, ratio * 1.25),
        "LoadData/L1↔L0 movement is dominant relative to MAC/Cube work.",
        _metric_rows(base, candidate, relative, (r"mte1", r"l[01].*bandwidth", r"(?:mac|cube).*ratio")),
        {"dominant_field": field, "mte1_ratio": _compact_number(ratio, 4)},
        ("LoadData repeated for Matmul/Conv A/B panels", "baseM/baseN/baseK or K slicing that destroys L1 reuse", "layout conversion or padding that reloads L0"),
        (_policy("increase_l1_l0_reuse", "Retile Matmul/Conv panels for L1/L0 reuse", preconditions=("panel reuse exists", "new tile fits L1/L0 and preserves alignment"), code_search=("LoadData", "Matmul", "baseM", "baseN", "baseK", "K loop"), expected=("MTE1 pressure decreases", "MAC/Cube share increases", "Event latency decreases"), fallback="retain parent tiling outside the verified matrix/conv domain"),),
        (_delta(r"mte1|l[01].*bandwidth", "decrease_or_more_efficient", "fewer panel reloads"), _delta(r"(?:mac|cube).*ratio", "increase_relative", "more useful compute per transfer")),
        (_check("panel_reload_count", "Are A/B panels reloaded before their reuse is exhausted?", ("LoadData offsets", "K/M/N loop nesting", "L1 capacity")), _check("tile_sweep", "Which baseM/baseN/baseK minimizes Event time, not only MTE1?", ("tiling sweep", "Event A/B", "correctness"))),
        ("A high MTE1 ratio can reflect intentionally low arithmetic intensity",),
    )
