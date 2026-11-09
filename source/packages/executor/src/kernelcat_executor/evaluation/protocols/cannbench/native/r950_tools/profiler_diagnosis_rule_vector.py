#!/usr/bin/env python3
"""Profiler diagnosis rule: vector."""
from __future__ import annotations

from typing import Any, Mapping

from profiler_diagnosis_rule_core import (
    _RuleResult, _check, _compact_number, _delta,
    _extreme_ratio, _metric_rows, _policy,
)

def _vector_rule(base: Mapping[str, Any], candidate: Mapping[str, Any], relative: Mapping[str, Any]) -> _RuleResult | None:
    field, vec = _extreme_ratio(candidate, (r"(?:^|[._])(?:aiv_)?vec(?:tor)?(?:_ratio)?$", r"vec.*ratio"))
    _, mte2 = _extreme_ratio(candidate, (r"mte2.*ratio",))
    if vec is None or vec < 0.65 or (mte2 or 0.0) >= 0.55:
        return None
    return _RuleResult(
        "vector_compute_pressure", "Vector compute pressure", "medium", min(1.0, vec),
        "Vector execution dominates while transfer pressure is not dominant; useful compute and redundant compute must be distinguished.",
        _metric_rows(base, candidate, relative, (r"vec.*ratio", r"mte2.*ratio", r"bank.*cflt")),
        {"dominant_field": field, "vector_ratio": _compact_number(vec, 4)},
        ("long Adds/Muls/Exp/Div/Cast chains", "recomputed intermediates or repeated transcendental operations", "scalarized tails or low vector width", "unnecessary precision conversions"),
        (
            _policy("eliminate_redundant_vector_work", "Remove repeated or algebraically redundant Vector operations", preconditions=("semantic analysis proves the repeated work is unnecessary", "numeric/correctness contract remains exact"), code_search=("Exp", "Div", "Cast", "Adds", "Muls", "same source/destination in repeated loops"), expected=("Vector instruction/work count decreases", "Event latency decreases", "vec ratio may decrease or remain high"), fallback="retain parent expression for numerically sensitive domains"),
            _policy("increase_vector_width_or_fuse_chain", "Use wider Vector repeats or legal in-kernel fusion", preconditions=("alignment/tail constraints permit wider repeats", "fusion does not increase UB conflict/pressure"), code_search=("mask", "repeat", "blkStride", "tail", "temporary LocalTensor"), expected=("instruction/repeat count decreases", "Event latency decreases"), fallback="retain scalar/tail parent path"),
        ),
        (_delta(r"vector.*instruction|vec.*work", "decrease_if_redundancy_removed", "less Vector work"), _delta(r"vec.*ratio", "ambiguous", "utilization can rise or fall after a real optimization")),
        (_check("algorithmic_work_count", "How many Vector operations are theoretically necessary per element?", ("source-level operation count", "tensor size", "algorithm contract")), _check("vector_width", "Are masks/repeats near hardware width or fragmented by tails?", ("repeat/mask histogram", "tail share", "alignment"))),
        ("High Vector utilization can be excellent or can mean ten redundant Exp calls",),
    )
