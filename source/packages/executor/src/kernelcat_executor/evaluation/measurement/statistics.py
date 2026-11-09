"""Loss-aware schemas for profiler-off NPU Event timing."""
from __future__ import annotations

import math
import statistics
from typing import Any, Iterable


TIMING_PROVENANCE_FIELDS = (
    "operator",
    "arm",
    "case_id",
    "run_id",
    "soc",
    "cann_version",
    "compiler_version",
    "dtype",
    "shape_domain",
)


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def summarize_event_samples_ms(elapsed_times: Iterable[float]) -> dict[str, Any]:
    samples = [float(value) for value in elapsed_times]
    if not samples or any(not math.isfinite(value) or value <= 0 for value in samples):
        raise ValueError("NPU Event samples must be finite positive milliseconds")
    p50 = _percentile(samples, 0.50)
    mean = statistics.fmean(samples)
    std = statistics.pstdev(samples)
    return {
        "schema_version": "npu-event-samples/v1",
        "timer": "npu_event",
        "profiler_enabled": False,
        "unit": "ms",
        "samples_ms": samples,
        # Backward-compatible evaluator keys; the loss-aware fields below are
        # the canonical source for new consumers.
        "mean": mean,
        "std": std,
        "min": min(samples),
        "max": max(samples),
        "num_trials": len(samples),
        "stats_ms": {
            "n": len(samples),
            "mean": mean,
            "p50": p50,
            "p95": _percentile(samples, 0.95),
            "p99": _percentile(samples, 0.99),
            "mad": _percentile([abs(value - p50) for value in samples], 0.50),
            "min": min(samples),
            "max": max(samples),
        },
    }


def make_profiler_off_timing_report(
    performance: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    """Convert evaluator milliseconds to the canonical microsecond report."""
    if (
        performance.get("schema_version") != "npu-event-samples/v1"
        or performance.get("timer") != "npu_event"
        or performance.get("profiler_enabled") is not False
        or performance.get("unit") != "ms"
    ):
        raise ValueError("input is not a verified profiler-off NPU Event sample set")
    missing = [field for field in TIMING_PROVENANCE_FIELDS if metadata.get(field) in (None, "")]
    if missing:
        raise ValueError("missing timing provenance: %s" % ", ".join(missing))
    samples_ms = performance.get("samples_ms") or []
    samples_us = [float(value) * 1000.0 for value in samples_ms]
    if len(samples_us) < 30:
        raise ValueError("profiler-off timing report requires at least 30 samples")
    return {
        "schema_version": "profiler-off-timing/v1",
        "profiler_enabled": False,
        "timer": "npu_event",
        "unit": "us",
        "samples_us": samples_us,
        "metadata": dict(metadata),
    }
