#!/usr/bin/env python3
"""Public Profiler bottleneck rule registry.

Counters map to suspects and disambiguation checks, never automatic rewrites.
"""
from __future__ import annotations

from typing import Any, Mapping

from profiler_diagnosis_rule_core import (
    AUTHORITY_CONTRACT, COMPACT_DIAGNOSIS_SCHEMA_VERSION,
    DIAGNOSIS_SCHEMA_VERSION, _EVENT_GATE, _GLOBAL_BLIND_SPOTS,
    _RuleResult, _canonical, _case_index, _compact_number, _mapping,
    _number, _sequence,
)
from profiler_diagnosis_rule_bank import _bank_conflict_rule
from profiler_diagnosis_rule_cube import _cube_rule
from profiler_diagnosis_rule_gm_read import _gm_read_rule
from profiler_diagnosis_rule_gm_write import _gm_write_rule
from profiler_diagnosis_rule_icache import _icache_rule
from profiler_diagnosis_rule_imbalance import _imbalance_rule
from profiler_diagnosis_rule_l2 import _l2_rule
from profiler_diagnosis_rule_launch import _launch_rule
from profiler_diagnosis_rule_mte1 import _mte1_rule
from profiler_diagnosis_rule_pipeline import _pipeline_rule
from profiler_diagnosis_rule_scalar import _scalar_rule
from profiler_diagnosis_rule_vector import _vector_rule


def _rule_results(
    feedback: Mapping[str, Any],
    comparison: Mapping[str, Any],
    event: Mapping[str, Any],
) -> list[_RuleResult]:
    base = _mapping(comparison.get("base"))
    candidate = _mapping(comparison.get("candidate"))
    relative = _mapping(comparison.get("relative_delta"))
    results: list[_RuleResult] = []
    for detector in (
        _bank_conflict_rule,
        _scalar_rule,
        _pipeline_rule,
        _gm_read_rule,
        _gm_write_rule,
        _mte1_rule,
        _l2_rule,
        _icache_rule,
        _imbalance_rule,
        _vector_rule,
        _cube_rule,
    ):
        result = detector(base, candidate, relative)
        if result is not None:
            results.append(result)
    launch = _launch_rule(
        feedback,
        str(comparison.get("run_id") or event.get("run_id") or ""),
        str(comparison.get("candidate_arm") or event.get("candidate_arm") or ""),
        int(_case_index(comparison.get("case_id")) or event.get("case_id") or -1),
        event,
    )
    if launch is not None:
        results.append(launch)
    return sorted(results, key=lambda item: (-item.score, item.rule_id))
