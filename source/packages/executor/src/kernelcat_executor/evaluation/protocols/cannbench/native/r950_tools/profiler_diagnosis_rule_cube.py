#!/usr/bin/env python3
"""Profiler diagnosis rule: cube."""
from __future__ import annotations

from typing import Any, Mapping

from profiler_diagnosis_rule_core import (
    _RuleResult, _check, _compact_number, _delta,
    _extreme_ratio, _metric_rows, _policy,
)

def _cube_rule(base: Mapping[str, Any], candidate: Mapping[str, Any], relative: Mapping[str, Any]) -> _RuleResult | None:
    field, cube = _extreme_ratio(candidate, (r"(?:^|[._])(?:aic_)?(?:mac|cube)(?:_ratio)?$", r"(?:mac|cube).*ratio"))
    _, mte2 = _extreme_ratio(candidate, (r"mte2.*ratio",))
    if cube is None or cube < 0.65 or (mte2 or 0.0) >= 0.55:
        return None
    return _RuleResult(
        "cube_compute_pressure", "Cube/MAC compute pressure", "medium", min(1.0, cube),
        "Cube/MAC execution dominates; tiling efficiency, padding, and unnecessary MACs are suspects.",
        _metric_rows(base, candidate, relative, (r"(?:mac|cube).*ratio", r"mte[12].*ratio")),
        {"dominant_field": field, "cube_ratio": _compact_number(cube, 4)},
        ("Matmul/Conv baseM/baseN/baseK under-utilization", "padding or masked MACs", "repeated Matmul setup or partial accumulation", "wrong Cube/Vector split"),
        (_policy("retile_cube_mainloop", "Retile Cube/Matmul/Conv main loop", preconditions=("shape domain supports the candidate tile", "L0/L1/UB footprint and alignment remain valid"), code_search=("Matmul", "Conv", "baseM", "baseN", "baseK", "K loop", "padding"), expected=("useful MAC density increases", "MTE1 per MAC decreases", "Event latency decreases"), fallback="retain parent tile outside the proven shape bucket"),),
        (_delta(r"(?:mac|cube).*ratio", "same_or_increase", "useful Cube occupancy may remain high"), _delta(r"mte1.*ratio|padding|wasted.*mac", "decrease", "better reuse and fewer wasted MACs")),
        (_check("useful_mac_ratio", "What fraction of issued MACs correspond to real output elements?", ("shape/padding", "issued MACs", "theoretical MACs")), _check("tile_sweep", "Which tile wins stable Event A/B?", ("baseM/N/K sweep", "resource footprint", "Event LCB"))),
        ("High Cube utilization cannot reveal whether MACs are mathematically necessary",),
    )
