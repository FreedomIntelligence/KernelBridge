#!/usr/bin/env python3
"""Profiler diagnosis rule: gm_read."""
from __future__ import annotations

import math

from typing import Any, Mapping

from profiler_diagnosis_rule_core import (
    _RuleResult, _check, _compact_number, _delta,
    _extreme_ratio, _metric_rows, _policy,
)

def _gm_read_rule(
    base: Mapping[str, Any], candidate: Mapping[str, Any], relative: Mapping[str, Any]
) -> _RuleResult | None:
    field, mte2 = _extreme_ratio(candidate, (r"(?:^|[._])(?:aiv_|aic_)?mte2(?:_ratio)?$", r"mte2.*ratio"))
    _, vec = _extreme_ratio(candidate, (r"(?:^|[._])(?:aiv_)?vec(?:tor)?(?:_ratio)?$", r"vec.*ratio"))
    _, cube = _extreme_ratio(candidate, (r"(?:^|[._])(?:aic_)?(?:mac|cube)(?:_ratio)?$", r"(?:mac|cube).*ratio"))
    if mte2 is None:
        return None
    compute = max(value for value in (vec, cube, 0.0) if value is not None)
    memory_bound = math.inf if compute <= 1e-12 and mte2 > 0 else mte2 / max(compute, 1e-12)
    if mte2 < 0.45 and memory_bound <= 1.0:
        return None
    return _RuleResult(
        rule_id="gm_read_pressure",
        title="GM→Core read-transfer pressure",
        confidence="high" if mte2 >= 0.65 and memory_bound > 1.0 else "medium",
        score=min(1.0, max(mte2, min(memory_bound / 2.0, 1.0))),
        mechanism="MTE2 transfer-pressure signature; it is not proof that DDR bandwidth is saturated.",
        matched=_metric_rows(base, candidate, relative, (r"mte2", r"(?:aiv_)?vec.*ratio", r"(?:mac|cube).*ratio")),
        derived={
            "dominant_field": field,
            "mte2_ratio": _compact_number(mte2, 4),
            "compute_ratio": _compact_number(compute, 4),
            "memory_bound": "inf" if math.isinf(memory_bound) else _compact_number(memory_bound, 4),
            "formula": "mte2_ratio / max(vec_ratio, cube_or_mac_ratio)",
        },
        suspect_code=(
            "DataCopy/CopyIn inside an inner loop whose source has a wider reuse domain",
            "small or non-contiguous DataCopy tiles with excessive issue/loop overhead",
            "tile ownership that reloads the same GM region across outputs or cores",
            "CopyIn→Compute serialized by queue depth, synchronization, or buffer lifetime",
        ),
        candidate_policies=(
            _policy(
                "eliminate_redundant_copyin",
                "Hoist or share repeated GM reads within the proven reuse domain",
                preconditions=("actual GM read bytes exceed theoretical necessary bytes", "the source region is immutable during reuse"),
                code_search=("DataCopy", "CopyIn", "inner output loop", "source offset", "reuse domain"),
                expected=("MTE2 ratio or read bytes decrease", "Event latency decreases", "Vector/Cube share may increase relatively"),
                fallback="retain parent loading outside the verified shape/capability domain",
            ),
            _policy(
                "increase_datacopy_granularity",
                "Coalesce small or strided copies into aligned larger transfers",
                preconditions=("bandwidth is low while MTE2 pressure is high", "copy sizes/strides prove latency or transaction inefficiency"),
                code_search=("DataCopy length", "tile size", "alignment", "loop trip count", "non-contiguous copy"),
                expected=("copy transaction count decreases", "effective read bandwidth increases", "Event latency decreases"),
                fallback="retain small-copy parent for tails or irregular layouts",
            ),
            _policy(
                "retain_reuse_in_ub_or_l1",
                "Keep reusable input resident in UB/L1",
                preconditions=("reuse distance fits UB/L1 capacity", "residency does not introduce bank conflict or harmful occupancy loss"),
                code_search=("TQue", "InitBuffer", "LocalTensor lifetime", "LoadData", "loop nesting"),
                expected=("GM/L1 reload count decreases", "MTE2/MTE1 pressure decreases", "Event latency decreases"),
                fallback="fall back to streamed parent when capacity or layout checks fail",
            ),
            _policy(
                "double_buffer_copy_compute",
                "Overlap tile N compute with tile N+1 CopyIn",
                preconditions=("timeline or wait/IQ evidence proves serialization", "multiple tiles exist", "copy is long enough to hide", "two buffers fit UB/L1 without reducing useful occupancy"),
                code_search=("TQue<..., 1>", "InitBuffer", "CopyIn", "Compute", "CopyOut", "barrier/event"),
                expected=("wait/stall decreases", "MTE2 and compute overlap increases", "Event latency decreases"),
                fallback="retain single buffering when copy is short, one tile fits, or UB pressure rises",
            ),
        ),
        expected_pmu_delta=(
            _delta(r"mte2|dram.*read|read.*bytes", "decrease_or_more_efficient", "less or better-amortized GM read work"),
            _delta(r"(?:aiv_)?vec.*ratio|(?:mac|cube).*ratio", "increase_relative_if_copy_removed", "compute occupies a larger fraction after transfer pressure falls"),
            _delta(r"wait|iq.*full|stall", "decrease_if_overlapped", "pipeline serialization is reduced"),
        ),
        disambiguation_checks=(
            _check("dram_bandwidth_ceiling", "Is read bandwidth near the device/workload ceiling?", ("DDR/HBM read bandwidth", "theoretical or calibrated ceiling", "active core count")),
            _check("actual_vs_theoretical_bytes", "Do actual GM read bytes exceed the minimum required by the algorithm?", ("input shape/dtype", "theoretical bytes", "measured read bytes")),
            _check("copy_granularity", "Are copies small, strided, non-contiguous, or misaligned?", ("copy-size histogram", "burst/alignment metadata", "DataCopy loop count")),
            _check("reuse_domain", "Is the same source region loaded repeatedly inside a narrower loop?", ("source offsets", "loop nesting", "tile ownership")),
            _check("copy_compute_overlap", "Are MTE2 and compute actually serialized?", ("timeline", "wait/IQ counters", "queue depth and synchronization")),
        ),
        blind_spots=("high MTE2 can be purely relative when useful compute is tiny", "counter data alone cannot choose among bandwidth, latency, redundancy, and serialization"),
    )
