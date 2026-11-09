#!/usr/bin/env python3
"""Profiler diagnosis rule: launch."""
from __future__ import annotations

from typing import Any, Mapping

from profiler_diagnosis_rule_core import (
    _RuleResult, _case_index, _check, _compact_number,
    _delta, _mapping, _number, _policy,
    _ratio, _sequence,
)

def _execution_context(feedback: Mapping[str, Any], run_id: str, arm: str, case_id: int) -> dict[str, Any]:
    best: dict[str, Any] = {}
    for item in _sequence(feedback.get("compact_feedback")):
        if not isinstance(item, Mapping) or str(item.get("run_id") or "") != run_id or str(item.get("arm") or "") != arm:
            continue
        compact = _mapping(item.get("feedback"))
        for case in _sequence(compact.get("cases")):
            if isinstance(case, Mapping) and _case_index(case.get("case_id")) == case_id:
                execution = _mapping(case.get("execution"))
                if execution:
                    best = dict(execution)
    return best


def _launch_rule(feedback: Mapping[str, Any], run_id: str, arm: str, case_id: int, event: Mapping[str, Any]) -> _RuleResult | None:
    execution = _execution_context(feedback, run_id, arm, case_id)
    launch_bound = _ratio(execution.get("launch_bound_ratio"))
    kernel_count = int(_number(execution.get("kernel_count")) or 0)
    candidate_us = _number(event.get("candidate_us"))
    short_many = candidate_us is not None and candidate_us <= 50.0 and kernel_count >= 2
    if (launch_bound is None or launch_bound < 0.25) and not short_many:
        return None
    matched = []
    for field in ("launch_bound_ratio", "kernel_count", "kernel_total_us"):
        if field in execution:
            matched.append({"field": f"execution.{field}", "candidate": _compact_number(execution.get(field), 4)})
    return _RuleResult(
        "launch_or_fragmentation_overhead", "Kernel launch / fragmented execution overhead", "medium", min(1.0, max(launch_bound or 0.0, 0.35 if short_many else 0.0)),
        "Fixed launch/setup or several short kernels consume a material share of end-to-end Event latency.",
        tuple(matched),
        {"candidate_event_us": _compact_number(candidate_us, 3), "kernel_count": kernel_count},
        ("blockDim or MIX kernel type larger than useful parallel work", "multiple short kernels/materialized intermediates", "heavy initialization in a microsecond-scale kernel"),
        (
            _policy("right_size_blockdim_and_kernel_type", "Use fewer cores or the minimal kernel type for short work", preconditions=("core-count/kernel-type sweep shows lower Event latency", "correctness and occupancy remain valid"), code_search=("SetBlockDim", "<<<blockDim", "Kernel Type", "MIX", "AIC", "AIV"), expected=("fixed startup share decreases", "Event latency decreases"), fallback="retain more cores for larger workloads"),
            _policy("fuse_short_kernel_chain", "Fuse short kernels to remove launch/GM boundaries", preconditions=("fusion is semantically legal", "resource footprint remains safe"), code_search=("multiple kernel launches", "intermediate GM tensor", "host dispatch"), expected=("kernel count and GM round trips decrease", "Event latency decreases"), fallback="retain unfused parent outside the proven capability domain"),
        ),
        (_delta(r"kernel_count|launch", "decrease", "less fixed overhead"), _delta(r"mte2|mte3", "decrease_if_fused", "fewer intermediate GM transfers")),
        (_check("core_count_sweep", "Does Event latency grow as blockDim grows?", ("1/4/8/16-core Event sweep", "per-core work")), _check("kernel_type", "Are unused AIC/AIV resources launched?", ("kernel type", "per-pipe activity", "launch configuration"))),
        ("Profiler cannot infer that two kernels are legally fusible",),
    )
