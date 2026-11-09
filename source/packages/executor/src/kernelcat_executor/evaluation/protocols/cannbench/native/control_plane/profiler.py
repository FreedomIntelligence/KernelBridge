"""Token-efficient profiler evidence with strict decision/mechanism separation."""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .models import ContractError, content_id


def _finite_samples(values: Iterable[float], name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if not result:
        raise ContractError(f"{name} samples are empty")
    if any(value <= 0 or not math.isfinite(value) for value in result):
        raise ContractError(f"{name} samples must be finite positive durations")
    return result


def percentile(values: Sequence[float], q: float) -> float:
    if not 0 <= q <= 1:
        raise ValueError(q)
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


@dataclass(frozen=True)
class DurationSummary:
    count: int
    p50_us: float
    p95_us: float
    p99_us: float
    mad_us: float
    min_us: float
    max_us: float

    @classmethod
    def from_samples(cls, samples: Sequence[float]) -> "DurationSummary":
        values = _finite_samples(samples, "duration")
        median = statistics.median(values)
        deviations = [abs(value - median) for value in values]
        return cls(
            count=len(values),
            p50_us=median,
            p95_us=percentile(values, 0.95),
            p99_us=percentile(values, 0.99),
            mad_us=statistics.median(deviations),
            min_us=min(values),
            max_us=max(values),
        )

    @property
    def tail_ratio(self) -> float:
        return self.p95_us / self.p50_us

    def compact(self) -> dict[str, Any]:
        return {
            "n": self.count,
            "p50_us": round(self.p50_us, 6),
            "p95_us": round(self.p95_us, 6),
            "p99_us": round(self.p99_us, 6),
            "mad_us": round(self.mad_us, 6),
            "p95_p50": round(self.tail_ratio, 6),
        }


@dataclass(frozen=True)
class EventABDecision:
    base: DurationSummary
    candidate: DurationSummary
    speedup_p50: float
    speedup_lcb95: float
    iterations: int
    seed: int
    same_device: bool
    profiler_enabled: bool
    correctness_pass: bool
    case_coverage: str
    base_build_id: str
    candidate_build_id: str
    accepted: bool
    rejection_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def decision_id(self) -> str:
        return content_id("eventab", self)

    def compact(self) -> dict[str, Any]:
        return {
            "decision": "accept" if self.accepted else "reject",
            "speedup_p50": round(self.speedup_p50, 6),
            "speedup_lcb95": round(self.speedup_lcb95, 6),
            "base": self.base.compact(),
            "candidate": self.candidate.compact(),
            "same_device": self.same_device,
            "profiler_enabled": self.profiler_enabled,
            "correctness_pass": self.correctness_pass,
            "case_coverage": self.case_coverage,
            "reasons": list(self.rejection_reasons),
            "provenance": {
                "base_build_id": self.base_build_id,
                "candidate_build_id": self.candidate_build_id,
                "bootstrap_iterations": self.iterations,
                "seed": self.seed,
            },
        }


def compare_event_samples(
    base_samples: Sequence[float],
    candidate_samples: Sequence[float],
    *,
    base_build_id: str,
    candidate_build_id: str,
    same_device: bool,
    profiler_enabled: bool,
    correctness_pass: bool,
    case_coverage: str,
    min_samples: int = 10,
    min_lcb: float = 1.0,
    max_tail_ratio: float = 1.25,
    iterations: int = 2000,
    seed: int = 0,
) -> EventABDecision:
    """Judge latency only from profiler-off Event samples."""
    base_values = _finite_samples(base_samples, "base")
    candidate_values = _finite_samples(candidate_samples, "candidate")
    if iterations < 200:
        raise ContractError("bootstrap iterations must be at least 200")
    base = DurationSummary.from_samples(base_values)
    candidate = DurationSummary.from_samples(candidate_values)
    speedup = base.p50_us / candidate.p50_us
    rng = random.Random(seed)
    ratios: list[float] = []
    for _ in range(iterations):
        sampled_base = [base_values[rng.randrange(len(base_values))] for _ in base_values]
        sampled_candidate = [candidate_values[rng.randrange(len(candidate_values))] for _ in candidate_values]
        ratios.append(statistics.median(sampled_base) / statistics.median(sampled_candidate))
    lcb = percentile(ratios, 0.025)
    reasons: list[str] = []
    if profiler_enabled:
        reasons.append("Event decision samples were collected with profiler enabled")
    if not same_device:
        reasons.append("base and candidate were not measured on the same device")
    if not correctness_pass:
        reasons.append("correctness did not pass before Event A/B")
    if min(len(base_values), len(candidate_values)) < min_samples:
        reasons.append(f"insufficient samples: require >= {min_samples} per arm")
    if base.tail_ratio > max_tail_ratio or candidate.tail_ratio > max_tail_ratio:
        reasons.append(f"unstable tail: p95/p50 must be <= {max_tail_ratio}")
    if lcb <= min_lcb:
        reasons.append(f"bootstrap 95% lower bound {lcb:.6f} must be > {min_lcb:.6f}")
    if not base_build_id.startswith("build_") or not candidate_build_id.startswith("build_"):
        reasons.append("compiled arm identities are missing")
    return EventABDecision(
        base=base,
        candidate=candidate,
        speedup_p50=speedup,
        speedup_lcb95=lcb,
        iterations=iterations,
        seed=seed,
        same_device=same_device,
        profiler_enabled=profiler_enabled,
        correctness_pass=correctness_pass,
        case_coverage=case_coverage,
        base_build_id=base_build_id,
        candidate_build_id=candidate_build_id,
        accepted=not reasons,
        rejection_reasons=tuple(reasons),
    )


_DURATION_KEYS = {
    "duration",
    "duration_us",
    "elapsed",
    "elapsed_us",
    "kernel_duration",
    "kernel_duration_us",
    "task_duration",
    "task_duration_us",
}


@dataclass(frozen=True)
class PMUSignal:
    name: str
    base: float
    candidate: float
    delta_ratio: float | None
    expected_direction: str
    direction_matches: bool | None

    def compact(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base": round(self.base, 6),
            "candidate": round(self.candidate, 6),
            "delta_pct": round(self.delta_ratio * 100, 4) if self.delta_ratio is not None else None,
            "expected": self.expected_direction,
            "matches": self.direction_matches,
        }


@dataclass(frozen=True)
class PMUMechanismView:
    base_build_id: str
    candidate_build_id: str
    metric_group: str
    signals: tuple[PMUSignal, ...]
    duration_fields_excluded: tuple[str, ...]
    sample_count: int

    @property
    def mechanism_id(self) -> str:
        return content_id("pmu", self)

    @property
    def decision_eligible(self) -> bool:
        return False

    def compact(self, limit: int = 8) -> dict[str, Any]:
        ranked = sorted(self.signals, key=lambda signal: abs(signal.delta_ratio or 0.0), reverse=True)[:limit]
        return {
            "role": "mechanism_only",
            "decision_eligible": False,
            "metric_group": self.metric_group,
            "signals": [signal.compact() for signal in ranked],
            "duration_fields_excluded": list(self.duration_fields_excluded),
            "sample_count": self.sample_count,
            "provenance": {
                "base_build_id": self.base_build_id,
                "candidate_build_id": self.candidate_build_id,
            },
        }


def _weighted_average(rows: Sequence[Mapping[str, Any]], key: str, weight_key: str) -> float | None:
    total_weight = 0.0
    weighted = 0.0
    for row in rows:
        value = row.get(key)
        weight = row.get(weight_key, 1.0)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and isinstance(weight, (int, float)) and not isinstance(weight, bool):
            value_f = float(value)
            weight_f = float(weight)
            if math.isfinite(value_f) and math.isfinite(weight_f) and weight_f > 0:
                weighted += value_f * weight_f
                total_weight += weight_f
    return weighted / total_weight if total_weight else None


def build_pmu_view(
    base_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    *,
    base_build_id: str,
    candidate_build_id: str,
    metric_group: str,
    expected_directions: Mapping[str, str] | None = None,
    weight_key: str = "duration_us",
) -> PMUMechanismView:
    if not base_rows or not candidate_rows:
        raise ContractError("PMU rows cannot be empty")
    expected = dict(expected_directions or {})
    keys = sorted(str(key) for key in set().union(*(row.keys() for row in [*base_rows, *candidate_rows])))
    excluded = tuple(key for key in keys if key.lower() in _DURATION_KEYS)
    signals: list[PMUSignal] = []
    for key in keys:
        if key in excluded or key == weight_key:
            continue
        base = _weighted_average(base_rows, key, weight_key)
        candidate = _weighted_average(candidate_rows, key, weight_key)
        if base is None or candidate is None:
            continue
        delta = None if base == 0 else (candidate - base) / abs(base)
        direction = expected.get(key, "observe")
        if direction not in {"increase", "decrease", "unchanged", "observe"}:
            raise ContractError(f"invalid expected direction for {key}: {direction}")
        if direction == "increase":
            matches: bool | None = candidate > base
        elif direction == "decrease":
            matches = candidate < base
        elif direction == "unchanged":
            matches = delta is not None and abs(delta) <= 0.02
        else:
            matches = None
        signals.append(PMUSignal(key, base, candidate, delta, direction, matches))
    return PMUMechanismView(
        base_build_id=base_build_id,
        candidate_build_id=candidate_build_id,
        metric_group=metric_group,
        signals=tuple(signals),
        duration_fields_excluded=excluded,
        sample_count=min(len(base_rows), len(candidate_rows)),
    )


def compact_unified_feedback(payload: Mapping[str, Any], *, max_pmu_signals: int = 8) -> dict[str, Any]:
    event = payload.get("event_decision") or payload.get("decision") or {}
    pmu = payload.get("pmu_mechanism") or payload.get("pmu") or {}
    gate = payload.get("gate") or {}
    compact_pmu = dict(pmu) if isinstance(pmu, Mapping) else {"raw_type": type(pmu).__name__}
    signals = compact_pmu.get("signals")
    if isinstance(signals, list):
        compact_pmu["signals"] = signals[:max_pmu_signals]
    return {
        "schema_version": 1,
        "event_decision": event,
        "pmu_mechanism": compact_pmu,
        "gate": gate,
        "raw_evidence_ref": payload.get("raw_evidence_ref") or payload.get("evidence_catalog_ref"),
    }
