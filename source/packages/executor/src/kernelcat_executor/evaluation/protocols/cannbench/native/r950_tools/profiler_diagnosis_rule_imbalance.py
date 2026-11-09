#!/usr/bin/env python3
"""Profiler diagnosis rule: imbalance."""
from __future__ import annotations

from typing import Any, Mapping

from profiler_diagnosis_rule_core import (
    _RuleResult, _check, _compact_number, _delta,
    _extreme_ratio, _metric_rows, _policy,
)

def _imbalance_rule(base: Mapping[str, Any], candidate: Mapping[str, Any], relative: Mapping[str, Any]) -> _RuleResult | None:
    field, imbalance = _extreme_ratio(candidate, (r"imbalance.*(?:ratio|cv)", r"(?:aic|aiv).*time.*cv", r"block.*time.*cv"))
    if imbalance is None or imbalance < 0.10:
        return None
    return _RuleResult(
        "inter_core_load_imbalance", "Inter-core load imbalance", "high", min(1.0, imbalance * 2.0),
        "Per-block/core work duration is uneven, so the slowest core determines completion.",
        _metric_rows(base, candidate, relative, (r"imbalance", r"(?:aic|aiv).*time", r"block.*time")),
        {"dominant_field": field, "imbalance": _compact_number(imbalance, 4)},
        ("GetBlockIdx ownership with uneven tile counts", "tail/remainder assigned to only a few blocks", "blockDim larger than useful parallel work"),
        (_policy("rebalance_block_ownership", "Redistribute tiles/tails across active cores", preconditions=("per-block histogram confirms imbalance", "new mapping preserves locality/synchronization"), code_search=("GetBlockIdx", "SetBlockDim", "start/end", "tail", "tile count"), expected=("block-time variance and max block time decrease", "Event latency decreases"), fallback="retain parent mapping for tiny workloads where launch overhead dominates"),),
        (_delta(r"imbalance|time.*cv|max.*block.*time", "decrease", "more even core completion"),),
        (_check("per_block_times", "Which block IDs dominate completion time?", ("block_id", "aic_time/aiv_time", "tile ownership")), _check("core_count_sweep", "Would fewer cores reduce startup while staying balanced?", ("1/4/8/16-core Event sweep", "per-core work"))),
        ("Aggregate pipe ratios can look normal while only a subset of cores does the work",),
    )
