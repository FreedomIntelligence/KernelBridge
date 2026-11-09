#!/usr/bin/env python3
"""Profiler diagnosis rule: l2."""
from __future__ import annotations

from typing import Any, Mapping

from profiler_diagnosis_rule_core import (
    _RuleResult, _check, _compact_number, _delta,
    _extreme_ratio, _metric_rows, _policy,
)

def _l2_rule(base: Mapping[str, Any], candidate: Mapping[str, Any], relative: Mapping[str, Any]) -> _RuleResult | None:
    miss_field, miss = _extreme_ratio(candidate, (r"l2.*miss", r"cache.*miss"))
    hit_field, hit = _extreme_ratio(candidate, (r"l2.*hit", r"cache.*hit"), minimum=True)
    if (miss is None or miss < 0.20) and (hit is None or hit > 0.70):
        return None
    severity = max(miss or 0.0, 1.0 - (hit if hit is not None else 1.0))
    return _RuleResult(
        "l2_locality_pressure", "L2 locality / cache-miss pressure", "medium", min(1.0, severity * 1.3),
        "The working-set order or core/tile partition produces weak L2 reuse and extra DRAM traffic.",
        _metric_rows(base, candidate, relative, (r"l2", r"cache.*(?:hit|miss)", r"dram.*read")),
        {"miss_field": miss_field, "miss_ratio": _compact_number(miss, 4), "hit_field": hit_field, "hit_ratio": _compact_number(hit, 4)},
        ("core partition that destroys spatial/temporal reuse", "tile traversal order larger than L2 working set", "same data consumed by distant cores/times"),
        (_policy("reorder_tiles_for_l2_locality", "Change core/tile traversal to improve L2 reuse", preconditions=("reuse exists at L2 scale", "new order preserves load balance and correctness"), code_search=("GetBlockIdx", "tile index", "N/H/W traversal", "core partition"), expected=("L2 hit rate increases", "DRAM read bytes decreases", "Event latency decreases"), fallback="retain parent order for workloads with no reusable working set"),),
        (_delta(r"l2.*miss|cache.*miss", "decrease", "fewer cache misses"), _delta(r"l2.*hit|cache.*hit", "increase", "better locality"), _delta(r"dram.*read", "decrease", "less memory traffic")),
        (_check("reuse_distance", "Does the intended reuse fit the effective L2 window?", ("working-set bytes", "tile order", "reuse distance")), _check("partition_ab", "Does an alternative core partition improve Event and L2 together?", ("partition A/B", "Event timing", "L2 metrics"))),
        ("L2 counters cannot infer the semantically best traversal order",),
    )
