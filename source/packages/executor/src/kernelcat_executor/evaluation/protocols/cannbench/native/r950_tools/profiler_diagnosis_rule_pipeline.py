#!/usr/bin/env python3
"""Profiler diagnosis rule: pipeline."""
from __future__ import annotations

from typing import Any, Mapping

from profiler_diagnosis_rule_core import (
    _RuleResult, _check, _compact_number, _delta,
    _metric_rows, _number, _policy, _ratio,
)

def _pipeline_rule(base: Mapping[str, Any], candidate: Mapping[str, Any], relative: Mapping[str, Any]) -> _RuleResult | None:
    rows = _metric_rows(base, candidate, relative, (r"wait", r"stall", r"iq.*full", r"queue.*full"))
    strongest = 0.0
    for row in rows:
        value = _number(row.get("candidate"))
        if value is None:
            continue
        normalized = _ratio(value)
        strongest = max(strongest, normalized if normalized is not None else min(value / 100.0, 1.0))
    if not rows or strongest < 0.10:
        return None
    return _RuleResult(
        "pipeline_wait_or_queue_stall", "Pipeline wait / issue-queue stall", "high" if strongest >= 0.3 else "medium", min(1.0, strongest * 1.5),
        "One or more pipelines wait or back-pressure an issue queue; source-level serialization/synchronization is suspected.",
        rows,
        {"strongest_normalized_wait_or_stall": _compact_number(strongest, 4)},
        ("CopyIn→Compute→CopyOut executed serially for each tile", "TQue depth/lifetime prevents overlap", "barrier/event synchronization too broad", "producer/consumer imbalance or queue full condition"),
        (
            _policy("pipeline_double_buffer", "Pipeline CopyIn/Compute/CopyOut with two safe tile buffers", preconditions=("timeline or wait/IQ evidence proves serialization", "at least two tiles exist", "two buffers fit local memory", "copy or compute duration is long enough to overlap"), code_search=("TQue<", "InitBuffer", "CopyIn", "Compute", "CopyOut", "barrier", "SetFlag/WaitFlag"), expected=("wait/IQ-full decreases", "pipe overlap increases", "Event latency decreases"), fallback="retain parent single-buffer path for one-tile or UB-constrained cases"),
            _policy("narrow_synchronization_scope", "Reduce unnecessary barriers or queue dependencies", preconditions=("dependency audit proves operations are independent",), code_search=("barrier", "SetFlag", "WaitFlag", "EnQue", "DeQue", "FreeTensor"), expected=("wait/stall decreases", "Event latency decreases"), fallback="retain conservative synchronization outside the verified dependency graph"),
        ),
        (_delta(r"wait|stall|iq.*full|queue.*full", "decrease", "less idle/back-pressure time"), _delta(r"mte2|vec|(?:mac|cube)", "more_overlap_not_necessarily_lower_ratio", "pipelines overlap rather than serialize")),
        (_check("timeline_overlap", "Do MTE2, Vector/Cube, and MTE3 overlap across adjacent tiles?", ("timeline", "tile boundaries", "queue events")), _check("buffer_capacity", "Can a second tile buffer fit without occupancy/bank-conflict regression?", ("UB/L1 allocation", "queue depth", "bank conflict"))),
        ("Wait counters localize a dependency but do not identify the exact InitBuffer or event line",),
    )
