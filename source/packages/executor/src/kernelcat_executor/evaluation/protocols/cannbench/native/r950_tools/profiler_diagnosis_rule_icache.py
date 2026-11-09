#!/usr/bin/env python3
"""Profiler diagnosis rule: icache."""
from __future__ import annotations

from typing import Any, Mapping

from profiler_diagnosis_rule_core import (
    _RuleResult, _check, _compact_number, _delta,
    _extreme_ratio, _metric_rows, _policy,
)

def _icache_rule(base: Mapping[str, Any], candidate: Mapping[str, Any], relative: Mapping[str, Any]) -> _RuleResult | None:
    field, miss = _extreme_ratio(candidate, (r"i.?cache.*miss", r"instruction.*cache.*miss"))
    if miss is None or miss < 0.05:
        return None
    return _RuleResult(
        "instruction_cache_pressure", "Instruction-cache/code-size pressure", "high", min(1.0, miss * 4.0),
        "Kernel code footprint or branch fan-out causes instruction-cache misses.",
        _metric_rows(base, candidate, relative, (r"i.?cache", r"instruction.*cache", r"branch")),
        {"dominant_field": field, "miss_ratio": _compact_number(miss, 4)},
        ("giant template instantiation", "over-unrolled loops", "too many fast paths/branches in one kernel", "large inline helper chains"),
        (_policy("shrink_kernel_code_footprint", "Reduce unrolling/template or split capability-specialized paths", preconditions=("code-size inspection identifies the hot footprint", "dispatch overhead is smaller than saved I-cache cost"), code_search=("#pragma unroll", "template", "if constexpr", "fast path", "inline"), expected=("I-cache miss decreases", "Event latency decreases"), fallback="retain monolithic parent where launch/dispatch overhead dominates"),),
        (_delta(r"i.?cache.*miss|instruction.*cache.*miss", "decrease", "smaller hot code footprint"),),
        (_check("code_size", "How large is generated hot code and which branches execute?", ("binary/disassembly size", "route trace", "branch coverage")),),
        ("Profiler cannot decide whether splitting a kernel costs more launch overhead than it saves",),
    )
