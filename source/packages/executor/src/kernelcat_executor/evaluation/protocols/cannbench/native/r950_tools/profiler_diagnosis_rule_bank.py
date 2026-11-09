#!/usr/bin/env python3
"""Profiler diagnosis rule: bank."""
from __future__ import annotations

from typing import Any, Mapping

from profiler_diagnosis_rule_core import (
    _RuleResult, _check, _compact_number, _delta,
    _extreme_ratio, _metric_rows, _policy,
)

def _bank_conflict_rule(base: Mapping[str, Any], candidate: Mapping[str, Any], relative: Mapping[str, Any]) -> _RuleResult | None:
    field, conflict = _extreme_ratio(candidate, (r"bankgroup.*cflt", r"bank.*cflt", r"bank.*conflict"))
    if conflict is None or conflict < 0.10:
        return None
    return _RuleResult(
        "ub_bank_conflict", "UB bank/bank-group conflict", "very_high", min(1.0, conflict * 2.5),
        "Vector operands map repeatedly to the same UB bank or bank group; stride/layout is the primary suspect.",
        _metric_rows(base, candidate, relative, (r"bankgroup.*cflt", r"bank.*cflt", r"bank.*conflict")),
        {"dominant_field": field, "conflict_ratio": _compact_number(conflict, 4)},
        ("UnaryRepeatParams/BinaryRepeatParams srcBlkStride or dstBlkStride", "srcRepStride/dstRepStride and repeat layout", "UB buffer base-address alignment or overlapping source/destination layout"),
        (
            _policy("repack_ub_stride_layout", "Change UB layout or Vector strides to avoid bank aliasing", preconditions=("conflict reproduces on the target path", "new layout respects API stride/range constraints"), code_search=("srcBlkStride", "dstBlkStride", "srcRepStride", "dstRepStride", "RepeatParams", "LocalTensor offset"), expected=("bank and bank-group conflict ratios decrease", "Vector issue/wait improves", "Event latency decreases"), fallback="retain parent layout for unsupported tails or shapes"),
            _policy("separate_or_align_ub_buffers", "Realign or separate UB buffers", preconditions=("base-address mapping contributes to the conflict", "extra padding fits UB"), code_search=("InitBuffer", "Get<", "LocalTensor", "offset", "alignment/padding"), expected=("bank conflict decreases", "Event latency decreases"), fallback="disable padding when UB capacity or occupancy regresses"),
        ),
        (_delta(r"bankgroup.*cflt|bank.*cflt|bank.*conflict", "decrease", "fewer conflicting UB accesses"), _delta(r"(?:aiv_)?vec.*ratio", "increase_or_same", "Vector issues with less conflict")),
        (_check("stride_bank_mapping", "Which blocks/repeats alias the same banks?", ("src/dst address", "blkStride", "repeatStride", "dtype and datablock width")), _check("layout_ab", "Does continuous-read/strided-write beat strided-read/continuous-write by Event?", ("isolated layout arms", "Event timing", "conflict counters"))),
        ("The counter localizes the resource but not the exact offending API call when several Vector operations share the kernel",),
    )
