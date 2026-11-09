#!/usr/bin/env python3
"""Profiler diagnosis rule: gm_write."""
from __future__ import annotations

from typing import Any, Mapping

from profiler_diagnosis_rule_core import (
    _RuleResult, _check, _compact_number, _delta,
    _extreme_ratio, _metric_rows, _policy,
)

def _gm_write_rule(base: Mapping[str, Any], candidate: Mapping[str, Any], relative: Mapping[str, Any]) -> _RuleResult | None:
    field, ratio = _extreme_ratio(candidate, (r"(?:^|[._])(?:aiv_|aic_)?mte3(?:_ratio)?$", r"mte3.*ratio", r"dram.*write.*ratio"))
    if ratio is None or ratio < 0.35:
        return None
    return _RuleResult(
        "gm_write_pressure", "Core→GM write-transfer pressure", "high" if ratio >= 0.6 else "medium", min(1.0, ratio * 1.25),
        "MTE3/write traffic occupies a material execution share; intermediate or fragmented writes are suspects.",
        _metric_rows(base, candidate, relative, (r"mte3", r"dram.*write", r"write.*bytes")),
        {"dominant_field": field, "write_ratio": _compact_number(ratio, 4)},
        ("CopyOut inside an inner accumulation/reduction loop", "partial results materialized to GM then reloaded", "small or repeated tail writes"),
        (
            _policy("defer_or_fuse_copyout", "Keep intermediates local and write final results once", preconditions=("intermediate lifetime fits local memory", "fusion preserves ordering/numerics"), code_search=("CopyOut", "DataCopy to GM", "partial result", "intermediate tensor"), expected=("MTE3/write bytes decrease", "kernel count or GM round trips decrease", "Event latency decreases"), fallback="retain parent materialization when residency or semantic constraints fail"),
            _policy("coalesce_gm_writes", "Coalesce fragmented/aligned output writes", preconditions=("write histogram proves small or strided transactions",), code_search=("CopyOut length", "tail", "stride", "output layout"), expected=("write transaction count decreases", "effective write bandwidth increases", "Event latency decreases"), fallback="retain original tail path for unsupported alignment"),
        ),
        (_delta(r"mte3|dram.*write|write.*bytes", "decrease_or_more_efficient", "fewer or larger output transfers"),),
        (_check("actual_vs_theoretical_write_bytes", "Do measured writes exceed one final output materialization?", ("output bytes", "measured write bytes", "intermediate GM tensors")), _check("write_granularity", "Are CopyOut operations small or strided?", ("copy-size histogram", "alignment", "tail frequency"))),
        ("Profiler cannot decide whether cross-kernel fusion is semantically legal",),
    )
